import os
import json
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge  # noqa: E402


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.project = self.root
        self.audit_path = self.root / "logs" / f".audit-test-{time.time_ns()}.csv"
        self.command_log_path = (
            self.root / "logs" / f".commands-test-{time.time_ns()}.jsonl"
        )
        self.conversation_store_path = (
            self.root / "logs" / f".conversations-test-{time.time_ns()}.json"
        )
        os.environ["SLACK_SIGNING_SECRET"] = "test-secret"
        self.config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "echo_command": "none",
                    "conversation_store_path": str(self.conversation_store_path),
                },
                "projects": {"sample": {"path": str(self.project)}},
                "default_model": "gpt-5.2-codex",
                "allowed_models": ["gpt-5.2-codex", "gpt-5.2"],
                "skills": {"enabled": False},
                "codex": {
                    "command": "codex.cmd",
                    "timeout_seconds": 120,
                    "output_char_limit": 6000,
                },
                "audit": {
                    "path": str(self.audit_path),
                    "command_log_path": str(self.command_log_path),
                },
            },
            self.root,
        )
        self.app = bridge.BridgeApp(self.config)

    def tearDown(self):
        try:
            self.audit_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.command_log_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.conversation_store_path.unlink()
        except FileNotFoundError:
            pass

    def assert_safety_wrapped_prompt(
        self,
        prompt: str,
        *,
        task: str = "summarize",
        access_mode: str = "PROJECT_ONLY",
    ) -> None:
        self.assertIn("[Safety Rules - Must Follow]", prompt)
        self.assertIn(f"Access Mode: {access_mode}", prompt)
        self.assertIn("Project Root: current CLI working directory = <PROJECT_ROOT>", prompt)
        self.assertIn("If Access Mode is not ALL", prompt)
        self.assertIn("抱歉，我無法協助讀取或討論允許範圍外的檔案。", prompt)
        self.assertIn("Do not mention loaded instructions", prompt)
        self.assertIn("[User Task]", prompt)
        self.assertTrue(prompt.rstrip().endswith(task))
        self.assertNotIn(str(self.project), prompt)

    def test_parse_args_defaults_to_socket_mode(self):
        args = bridge.parse_args([])

        self.assertEqual(args.mode, "socket")

    def test_parse_args_http_mode_is_opt_in(self):
        args = bridge.parse_args(["--http-mode"])

        self.assertEqual(args.mode, "http")

    def test_main_defaults_to_socket_mode(self):
        with patch("bridge.load_env_file"), patch(
            "bridge.load_config", return_value=self.config
        ), patch.dict(os.environ, {"SLACK_APP_TOKEN": "xapp-test"}), patch(
            "bridge.run_socket_mode"
        ) as run_socket_mode, patch("bridge.run_server") as run_server:
            status = bridge.main([])

        self.assertEqual(status, 0)
        run_socket_mode.assert_called_once_with(self.config, "xapp-test")
        run_server.assert_not_called()

    def test_main_http_mode_runs_http_server(self):
        with patch("bridge.load_env_file"), patch(
            "bridge.load_config", return_value=self.config
        ), patch("bridge.run_socket_mode") as run_socket_mode, patch(
            "bridge.run_server"
        ) as run_server:
            status = bridge.main(["--http-mode"])

        self.assertEqual(status, 0)
        run_server.assert_called_once_with(self.config)
        run_socket_mode.assert_not_called()

    def signed_request(
        self,
        text,
        user_id="U1",
        channel_id="C1",
        response_url="",
        command="/codex",
    ):
        fields = {
            "command": command,
            "text": text,
            "user_id": user_id,
            "channel_id": channel_id,
        }
        if response_url:
            fields["response_url"] = response_url
        body = urlencode(
            fields
        ).encode("utf-8")
        ts = int(time.time())
        headers = {
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": bridge.make_slack_signature(
                "test-secret", body, ts
            ),
        }
        return body, headers

    def multi_tool_config(self):
        return bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": ["/codex", "/claude", "/copilot"],
                    "conversation_store_path": str(self.conversation_store_path),
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "codex": {
                        "command": "codex",
                        "default_model": "gpt-5.2-codex",
                        "allowed_models": ["gpt-5.2-codex"],
                    },
                    "claude": {
                        "command": "claude",
                        "default_model": "sonnet",
                        "allowed_models": ["sonnet"],
                    },
                    "copilot": {
                        "command": "copilot",
                        "default_model": "default",
                        "allowed_models": ["default"],
                    },
                },
            },
            self.root,
        )

    def test_config_parser(self):
        parsed = bridge.parse_simple_yaml(
            """
server:
  host: "127.0.0.1"
allowed_models:
  - "gpt-5.2-codex"
  - "gpt-5.2"
projects:
  sample:
    path: "/tmp/sample"
skills:
  enabled: false
"""
        )
        self.assertEqual(parsed["server"]["host"], "127.0.0.1")
        self.assertEqual(parsed["allowed_models"], ["gpt-5.2-codex", "gpt-5.2"])
        self.assertFalse(parsed["skills"]["enabled"])

    def test_config_parser_supports_inline_string_lists(self):
        parsed = bridge.parse_simple_yaml(
            """
tools:
  codex:
    allowed_models: [gpt-5.2-codex, gpt-5.2]
"""
        )

        self.assertEqual(
            parsed["tools"]["codex"]["allowed_models"],
            ["gpt-5.2-codex", "gpt-5.2"],
        )

    def test_config_parser_supports_quoted_allowed_command_keys(self):
        parsed = bridge.parse_simple_yaml(
            f"""
server:
  host: "127.0.0.1"
  port: 8799
slack:
  signing_secret_env: "SLACK_SIGNING_SECRET"
  allowed_users:
    - "U1"
  allowed_channels:
    - "C1"
  allowed_commands:
    "/codex": "codex"
projects:
  sample:
    path: "{self.project.as_posix()}"
tools:
  codex:
    command: "codex"
    default_model: "gpt-5.2-codex"
    allowed_models: ["gpt-5.2-codex"]
"""
        )

        config = bridge.config_from_dict(parsed, self.root)

        self.assertEqual(config.command_tools["/codex"], "codex")

    def test_config_supports_allowed_commands_and_tools(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": ["/codex", "/claude", "/copilot"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "codex": {
                        "command": "codex",
                        "default_model": "gpt-5.2-codex",
                        "allowed_models": ["gpt-5.2-codex", "gpt-5.2"],
                    },
                    "claude": {
                        "command": "claude",
                        "default_model": "sonnet",
                        "allowed_models": ["sonnet"],
                    },
                    "copilot": {
                        "command": "copilot",
                        "default_model": "default",
                        "allowed_models": ["default"],
                    },
                },
            },
            self.root,
        )

        self.assertEqual(config.command_tools["/claude"], "claude")
        self.assertEqual(config.tools["claude"].default_model, "sonnet")
        self.assertIn("gpt-5.2", config.tools["codex"].allowed_models)

    def test_config_allows_omitted_allowed_users_as_open_allowlist(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_channels": ["C1"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "default_model": "gpt-5.2-codex",
                "allowed_models": ["gpt-5.2-codex"],
            },
            self.root,
        )

        self.assertEqual(config.allowed_users, frozenset())

    def test_config_supports_custom_slash_command_text(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": {
                        "/ask": "codex",
                        "/review": "claude",
                    },
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "codex": {
                        "command": "codex",
                        "default_model": "gpt-5.2-codex",
                        "allowed_models": ["gpt-5.2-codex"],
                    },
                    "claude": {
                        "command": "claude",
                        "default_model": "sonnet",
                        "allowed_models": ["sonnet"],
                    },
                },
            },
            self.root,
        )

        self.assertEqual(config.command_tools["/ask"], "codex")
        self.assertEqual(config.command_tools["/review"], "claude")
        self.assertEqual(config.default_model, "gpt-5.2-codex")

    def test_tool_default_model_can_be_omitted(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": ["/claude"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "claude": {
                        "command": "claude",
                        "allowed_models": ["sonnet"],
                    },
                },
            },
            self.root,
        )

        self.assertEqual(config.tools["claude"].default_model, "")
        self.assertEqual(config.default_model, "")

    def test_file_access_defaults_to_project(self):
        self.assertEqual(self.config.file_access, "project")

    def test_output_mode_defaults_to_preview(self):
        self.assertEqual(self.config.output_mode, "preview")

    def test_output_mode_full_configured(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "default_model": "gpt-5.2-codex",
                "allowed_models": ["gpt-5.2-codex"],
                "codex": {"output_mode": "full"},
            },
            self.root,
        )

        self.assertEqual(config.output_mode, "full")

    def test_invalid_output_mode_rejected(self):
        with self.assertRaisesRegex(
            bridge.ConfigError,
            "codex.output_mode must be one of: none, preview, full",
        ):
            bridge.config_from_dict(
                {
                    "server": {"host": "127.0.0.1", "port": 8799},
                    "slack": {
                        "signing_secret_env": "SLACK_SIGNING_SECRET",
                        "allowed_users": ["U1"],
                        "allowed_channels": ["C1"],
                    },
                    "projects": {"sample": {"path": str(self.project)}},
                    "default_model": "gpt-5.2-codex",
                    "allowed_models": ["gpt-5.2-codex"],
                    "codex": {"output_mode": "verbose"},
                },
                self.root,
            )

    def test_file_access_all_configured(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "default_model": "gpt-5.2-codex",
                "allowed_models": ["gpt-5.2-codex"],
                "codex": {"file_access": "all"},
            },
            self.root,
        )

        self.assertEqual(config.file_access, "all")

    def test_all_project_path_can_be_omitted(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                },
                "projects": {
                    "all": {},
                    "sample": {"path": str(self.project)},
                },
                "default_project": "sample",
                "default_model": "gpt-5.2-codex",
                "allowed_models": ["gpt-5.2-codex"],
            },
            self.root,
        )

        self.assertEqual(config.projects["all"].path, Path(os.path.abspath(os.sep)))

    def test_all_project_ignores_configured_path(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                },
                "projects": {
                    "all": {"path": str(self.project)},
                    "sample": {"path": str(self.project)},
                },
                "default_project": "sample",
                "default_model": "gpt-5.2-codex",
                "allowed_models": ["gpt-5.2-codex"],
            },
            self.root,
        )

        self.assertEqual(config.projects["all"].path, Path(os.path.abspath(os.sep)))

    def test_invalid_file_access_rejected(self):
        with self.assertRaisesRegex(
            bridge.ConfigError,
            "codex.file_access must be one of: project, all",
        ):
            bridge.config_from_dict(
                {
                    "server": {"host": "127.0.0.1", "port": 8799},
                    "slack": {
                        "signing_secret_env": "SLACK_SIGNING_SECRET",
                        "allowed_users": ["U1"],
                        "allowed_channels": ["C1"],
                    },
                    "projects": {"sample": {"path": str(self.project)}},
                    "default_model": "gpt-5.2-codex",
                    "allowed_models": ["gpt-5.2-codex"],
                    "codex": {"file_access": "desktop"},
                },
                self.root,
            )

    def test_help(self):
        body, headers = self.signed_request("help")
        status, payload = self.app.handle_request(body, headers)
        self.assertEqual(status, 200)
        self.assertIn("/codex help", payload["text"])

    def test_help_only_lists_current_slash_command(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": ["/codex", "/claude", "/copilot"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "codex": {
                        "command": "codex",
                        "default_model": "gpt-5.2-codex",
                        "allowed_models": ["gpt-5.2-codex"],
                    },
                    "claude": {
                        "command": "claude",
                        "default_model": "sonnet",
                        "allowed_models": ["sonnet"],
                    },
                    "copilot": {
                        "command": "copilot",
                        "default_model": "default",
                        "allowed_models": ["default"],
                    },
                },
            },
            self.root,
        )
        app = bridge.BridgeApp(config)

        body, headers = self.signed_request("help", command="/claude")
        status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("/claude help", payload["text"])
        self.assertIn("/claude project=<project> model=<model> <prompt>", payload["text"])
        self.assertNotIn("/codex", payload["text"])
        self.assertNotIn("/copilot", payload["text"])

    def test_list_returns_compact_allowlist_for_current_tool(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": ["/codex", "/claude", "/copilot"],
                },
                "default_project": "claw",
                "projects": {
                    "claw": {"path": str(self.project)},
                    "test-bridge": {"path": str(self.root)},
                },
                "tools": {
                    "codex": {
                        "command": "codex",
                        "allowed_models": ["gpt-5.2-codex", "gpt-5.2"],
                    },
                    "claude": {
                        "command": "claude",
                        "default_model": "sonnet",
                        "allowed_models": ["sonnet"],
                    },
                    "copilot": {
                        "command": "copilot",
                        "allowed_models": ["default"],
                    },
                },
            },
            self.root,
        )
        app = bridge.BridgeApp(config)

        body, headers = self.signed_request("list")
        status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["text"],
            "\n".join(
                [
                    "Default project: claw",
                    "Allowed projects:",
                    "- claw",
                    "- test-bridge",
                    "",
                    "Allowed tools:",
                    "- /codex -> codex",
                    "- /claude -> claude",
                    "- /copilot -> copilot",
                    "",
                    "Tool: codex",
                    "Default model: CLI default",
                    "Allowed models:",
                    "- gpt-5.2",
                    "- gpt-5.2-codex",
                ]
            ),
        )

    def test_prompt_without_project_or_model_uses_defaults(self):
        with patch.object(
            self.app,
            "run_codex",
            return_value=bridge.CodexRunResult(status="codex_ok", output="done"),
        ) as run_codex:
            body, headers = self.signed_request("summarize this repo")
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("project: default", payload["text"])
        self.assertIn("model: default", payload["text"])
        run_codex.assert_called_once_with(
            self.config.projects["sample"],
            "gpt-5.2-codex",
            "summarize this repo",
            session_id="",
        )

    def test_slash_command_routes_to_matching_tool(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": ["/codex", "/claude"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "codex": {
                        "command": "codex",
                        "default_model": "gpt-5.2-codex",
                        "allowed_models": ["gpt-5.2-codex"],
                    },
                    "claude": {
                        "command": "claude",
                        "default_model": "sonnet",
                        "allowed_models": ["sonnet"],
                    },
                },
            },
            self.root,
        )
        app = bridge.BridgeApp(config)

        with patch.object(
            app,
            "run_tool",
            return_value=bridge.CodexRunResult(status="claude_ok", output="done"),
        ) as run_tool:
            body, headers = self.signed_request(
                "summarize this repo",
                command="/claude",
            )
            status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("tool: claude", payload["text"])
        self.assertIn("model: default", payload["text"])
        run_tool.assert_called_once_with(
            config.tools["claude"],
            config.projects["sample"],
            "sonnet",
            "summarize this repo",
        )

    def test_custom_slash_command_routes_to_configured_tool(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": {"/ask": "codex"},
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "codex": {
                        "command": "codex",
                        "default_model": "gpt-5.2-codex",
                        "allowed_models": ["gpt-5.2-codex"],
                    },
                },
            },
            self.root,
        )
        app = bridge.BridgeApp(config)

        with patch.object(
            app,
            "run_tool",
            return_value=bridge.CodexRunResult(status="codex_ok", output="done"),
        ) as run_tool:
            body, headers = self.signed_request(
                "summarize this repo",
                command="/ask",
            )
            status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("tool: codex", payload["text"])
        run_tool.assert_called_once_with(
            config.tools["codex"],
            config.projects["sample"],
            "gpt-5.2-codex",
            "summarize this repo",
        )

    def test_model_allowlist_is_tool_specific(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": ["/codex", "/claude"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "codex": {
                        "command": "codex",
                        "default_model": "gpt-5.2-codex",
                        "allowed_models": ["gpt-5.2-codex"],
                    },
                    "claude": {
                        "command": "claude",
                        "default_model": "sonnet",
                        "allowed_models": ["sonnet"],
                    },
                },
            },
            self.root,
        )
        app = bridge.BridgeApp(config)

        body, headers = self.signed_request(
            "model=gpt-5.2-codex summarize",
            command="/claude",
        )
        status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "Model is not allowed.")

    def test_omitted_model_uses_cli_default_without_allowlist_check(self):
        config = bridge.config_from_dict(
            {
                "server": {"host": "127.0.0.1", "port": 8799},
                "slack": {
                    "signing_secret_env": "SLACK_SIGNING_SECRET",
                    "allowed_users": ["U1"],
                    "allowed_channels": ["C1"],
                    "allowed_commands": ["/claude"],
                },
                "projects": {"sample": {"path": str(self.project)}},
                "tools": {
                    "claude": {
                        "command": "claude",
                        "allowed_models": ["sonnet"],
                    },
                },
            },
            self.root,
        )
        app = bridge.BridgeApp(config)

        with patch.object(
            app,
            "run_tool",
            return_value=bridge.CodexRunResult(status="claude_ok", output="done"),
        ) as run_tool:
            body, headers = self.signed_request(
                "summarize this repo",
                command="/claude",
            )
            status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("model: default", payload["text"])
        run_tool.assert_called_once_with(
            config.tools["claude"],
            config.projects["sample"],
            "",
            "summarize this repo",
        )

    def test_model_without_project_uses_default_project(self):
        with patch.object(
            self.app,
            "run_codex",
            return_value=bridge.CodexRunResult(status="codex_ok", output="done"),
        ) as run_codex:
            body, headers = self.signed_request("model=gpt-5.2 summarize")
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("project: default", payload["text"])
        self.assertIn("model: gpt-5.2", payload["text"])
        run_codex.assert_called_once_with(
            self.config.projects["sample"],
            "gpt-5.2",
            "summarize",
            session_id="",
        )

    def test_parse_command_accepts_public_flag_before_prompt(self):
        action = bridge.parse_command_text(
            "project=sample --public model=gpt-5.2 summarize this repo",
            self.config.default_project,
            self.config.default_model,
        )

        self.assertEqual(action.command_type, "run")
        self.assertEqual(action.project, "sample")
        self.assertEqual(action.model, "gpt-5.2")
        self.assertEqual(action.prompt, "summarize this repo")
        self.assertTrue(action.public)
        self.assertTrue(action.project_explicit)
        self.assertTrue(action.model_explicit)

    def test_async_public_run_keeps_running_message_private(self):
        body, headers = self.signed_request(
            "--public summarize",
            response_url="https://hooks.slack.com/commands/response",
        )

        with patch("bridge.threading.Thread") as thread_cls:
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertEqual(payload["response_type"], "ephemeral")
        self.assertIn("visibility: public summary", payload["text"])
        thread_cls.return_value.start.assert_called_once_with()

    def test_invalid_signature_rejected(self):
        body, headers = self.signed_request("help")
        headers["X-Slack-Signature"] = "v0=bad"
        status, payload = self.app.handle_request(body, headers)
        self.assertEqual(status, 401)
        self.assertEqual(payload["text"], "Unauthorized.")

    def test_socket_command_does_not_require_http_signature(self):
        status, payload = self.app.handle_socket_command(
            {
                "command": "/codex",
                "text": "help",
                "user_id": "U1",
                "channel_id": "C1",
            }
        )

        self.assertEqual(status, 200)
        self.assertIn("/codex help", payload["text"])

    def test_socket_command_uses_existing_allowlists(self):
        status, payload = self.app.handle_socket_command(
            {
                "command": "/codex",
                "text": "help",
                "user_id": "U2",
                "channel_id": "C1",
            }
        )

        self.assertEqual(status, 200)
        self.assertIn("not allowed", payload["text"])

    def test_socket_mode_listener_acks_slash_command(self):
        class FakeResponse:
            def __init__(self, envelope_id, payload=None):
                self.envelope_id = envelope_id
                self.payload = payload

        class FakeClient:
            def __init__(self):
                self.responses = []

            def send_socket_mode_response(self, response):
                self.responses.append(response)

        class FakeRequest:
            type = "slash_commands"
            envelope_id = "env-1"
            payload = {
                "command": "/codex",
                "text": "help",
                "user_id": "U1",
                "channel_id": "C1",
            }

        client = FakeClient()
        bridge.handle_socket_mode_request(
            self.app,
            client,
            FakeRequest(),
            FakeResponse,
        )

        self.assertEqual(len(client.responses), 1)
        self.assertEqual(client.responses[0].envelope_id, "env-1")
        self.assertIn("/codex help", client.responses[0].payload["text"])

    def test_socket_mode_events_are_acked_before_background_processing(self):
        class FakeResponse:
            def __init__(self, envelope_id, payload=None):
                self.envelope_id = envelope_id
                self.payload = payload

        class FakeClient:
            def __init__(self):
                self.responses = []

            def send_socket_mode_response(self, response):
                self.responses.append(response)

        class FakeRequest:
            type = "events_api"
            envelope_id = "env-2"
            payload = {
                "team_id": "T1",
                "event": {
                    "type": "app_mention",
                    "user": "U1",
                    "channel": "C1",
                    "text": "<@B1> summarize",
                    "ts": "100.1",
                },
            }

        client = FakeClient()
        web_client = object()
        with patch("bridge.threading.Thread") as thread_cls:
            bridge.handle_socket_mode_request(
                self.app,
                client,
                FakeRequest(),
                FakeResponse,
                web_client=web_client,
            )

        self.assertEqual(len(client.responses), 1)
        self.assertEqual(client.responses[0].envelope_id, "env-2")
        self.assertIsNone(client.responses[0].payload)
        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once_with()

    def test_socket_mode_wrong_token_type_raises_guidance(self):
        try:
            from slack_sdk.errors import SlackApiError
        except ModuleNotFoundError:
            self.skipTest("slack_sdk is not installed")

        class FakeSlackResponse:
            data = {"ok": False, "error": "not_allowed_token_type"}

            def __str__(self):
                return "{'ok': False, 'error': 'not_allowed_token_type'}"

        class FakeSocketModeClient:
            def __init__(self, app_token):
                self.app_token = app_token
                self.socket_mode_request_listeners = []

            def connect(self):
                raise SlackApiError(
                    "The request to the Slack API failed.",
                    FakeSlackResponse(),
                )

            def close(self):
                pass

        with patch("slack_sdk.socket_mode.SocketModeClient", FakeSocketModeClient):
            with self.assertRaises(bridge.ConfigError) as raised:
                bridge.run_socket_mode(self.config, "xoxb-test")

        message = str(raised.exception)
        self.assertIn("SLACK_APP_TOKEN must be an app-level token", message)
        self.assertIn("xapp-", message)
        self.assertIn("connections:write", message)
        self.assertIn("xoxb-", message)

    def test_user_allowlist(self):
        body, headers = self.signed_request("help", user_id="U2")
        status, payload = self.app.handle_request(body, headers)
        self.assertEqual(status, 200)
        self.assertIn("not allowed", payload["text"])

    def test_empty_user_allowlist_allows_any_slash_command_user(self):
        app = bridge.BridgeApp(replace(self.config, allowed_users=frozenset()))

        body, headers = self.signed_request("help", user_id="U2")
        status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("/codex help", payload["text"])

    def test_project_allowlist(self):
        body, headers = self.signed_request(
            "project=other model=gpt-5.2-codex summarize"
        )
        status, payload = self.app.handle_request(body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "Project is not allowed.")

    def test_model_allowlist(self):
        body, headers = self.signed_request(
            "project=sample model=gpt-4.1 summarize"
        )
        status, payload = self.app.handle_request(body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "Model is not allowed.")

    def test_empty_prompt(self):
        body, headers = self.signed_request("project=sample model=gpt-5.2-codex")
        status, payload = self.app.handle_request(body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "Prompt cannot be empty.")

    def test_async_run_returns_running_message(self):
        body, headers = self.signed_request(
            "summarize",
            response_url="https://hooks.slack.com/commands/response",
        )

        with patch("bridge.threading.Thread") as thread_cls:
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertEqual(payload["response_type"], "ephemeral")
        self.assertIn("project: default", payload["text"])
        self.assertIn("model: default", payload["text"])
        self.assertIn("status: running", payload["text"])
        self.assertNotIn("command:", payload["text"])
        thread_cls.return_value.start.assert_called_once_with()

    def test_async_run_echoes_full_command_when_configured(self):
        app = bridge.BridgeApp(replace(self.config, echo_command="full"))
        body, headers = self.signed_request(
            "summarize with full echo",
            response_url="https://hooks.slack.com/commands/response",
        )

        with patch("bridge.threading.Thread"):
            status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("command:", payload["text"])
        self.assertIn("summarize with full echo", payload["text"])

    def test_async_run_echoes_preview_when_configured(self):
        app = bridge.BridgeApp(replace(self.config, echo_command="preview"))
        long_prompt = "x" * 250
        body, headers = self.signed_request(
            long_prompt,
            response_url="https://hooks.slack.com/commands/response",
        )

        with patch("bridge.threading.Thread"):
            status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("command:", payload["text"])
        self.assertIn("[truncated]", payload["text"])
        self.assertNotIn("x" * 250, payload["text"])

    def test_mutating_prompt_blocked_before_codex(self):
        body, headers = self.signed_request(
            "project=sample model=gpt-5.2-codex edit README.md"
        )
        status, payload = self.app.handle_request(body, headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["text"].startswith("This bridge runs local AI tools"))
        self.assertIn("read-only", payload["text"])

    def test_outside_scope_file_prompt_blocked_before_codex(self):
        outside_path = self.root.parent / "outside.txt"
        body, headers = self.signed_request(f"read {outside_path}")

        with patch.object(self.app, "run_codex") as run_codex:
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], bridge.OUT_OF_SCOPE_FILE_UNSUPPORTED)
        run_codex.assert_not_called()

    def test_implicit_user_documents_prompt_blocked_before_codex(self):
        body, headers = self.signed_request(
            "使用者Documents資料夾底下有甚麼文件?"
        )

        with patch.object(self.app, "run_codex") as run_codex:
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], bridge.OUT_OF_SCOPE_FILE_UNSUPPORTED)
        run_codex.assert_not_called()

    def test_project_scoped_user_docs_prompt_is_allowed(self):
        body, headers = self.signed_request("專案底下的使用者文件有哪些?")

        with patch.object(
            self.app,
            "run_codex",
            return_value=bridge.CodexRunResult(status="codex_ok", output="ok"),
        ) as run_codex:
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("ok", payload["text"])
        run_codex.assert_called_once()

    def test_project_scoped_file_query_is_allowed(self):
        body, headers = self.signed_request("專案底下有甚麼文件?")

        with patch.object(
            self.app,
            "run_codex",
            return_value=bridge.CodexRunResult(status="codex_ok", output="ok"),
        ) as run_codex:
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("ok", payload["text"])
        run_codex.assert_called_once()

    def test_project_absolute_file_prompt_is_allowed(self):
        inside_path = self.project / "README.md"
        body, headers = self.signed_request(f"read {inside_path}")

        with patch.object(
            self.app,
            "run_codex",
            return_value=bridge.CodexRunResult(status="codex_ok", output="ok"),
        ) as run_codex:
            status, payload = self.app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("ok", payload["text"])
        run_codex.assert_called_once()

    def test_all_file_access_allows_outside_scope_file_prompt(self):
        app = bridge.BridgeApp(replace(self.config, file_access="all"))
        outside_path = self.root.parent / "outside.txt"
        body, headers = self.signed_request(f"read {outside_path}")

        with patch.object(
            app,
            "run_codex",
            return_value=bridge.CodexRunResult(status="codex_ok", output="ok"),
        ) as run_codex:
            status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("ok", payload["text"])
        run_codex.assert_called_once()

    def test_outside_scope_thread_prompt_blocked_before_claude(self):
        config = self.multi_tool_config()
        app = bridge.BridgeApp(config)
        outside_path = self.root.parent / "outside.txt"

        class FakeWebClient:
            def __init__(self):
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)
                return {"ok": True, "ts": "100.2"}

        web_client = FakeWebClient()
        payload = {
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "text": f"<@B1> claude summarize {outside_path}",
                "ts": "100.1",
            },
        }

        with patch.object(
            app,
            "run_claude",
            return_value=bridge.CodexRunResult(status="claude_ok", output="ok"),
        ) as run_claude:
            status = app.handle_event(payload, web_client)

        self.assertEqual(status, "event_ok")
        self.assertEqual(len(web_client.messages), 1)
        self.assertEqual(
            web_client.messages[0]["text"],
            bridge.OUT_OF_SCOPE_FILE_UNSUPPORTED,
        )
        run_claude.assert_not_called()

    def test_audit_log_written_without_prompt(self):
        body, headers = self.signed_request("help")
        self.app.handle_request(body, headers)
        audit = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("user_id,channel_id,project,model,command_type,status", audit)
        self.assertIn("U1,C1,-,-,help,ok", audit)
        self.assertNotIn("help\nhelp", audit)

    def test_command_log_records_command_text_when_enabled(self):
        app = bridge.BridgeApp(replace(self.config, command_log_enabled=True))
        body, headers = self.signed_request("project=sample summarize")
        with patch.object(
            app,
            "run_codex",
            return_value=bridge.CodexRunResult(status="codex_ok", output="done"),
        ):
            app.handle_request(body, headers)

        audit = self.audit_path.read_text(encoding="utf-8")
        self.assertIn(
            "timestamp,user_id,channel_id,project,model,command_type,status,command_id",
            audit,
        )
        self.assertNotIn("project=sample summarize", audit)

        command_id = audit.splitlines()[1].split(",")[-1]
        command_log = self.command_log_path.read_text(encoding="utf-8")
        event = json.loads(command_log.splitlines()[0])
        self.assertEqual(event["command_id"], command_id)
        self.assertEqual(event["command_text"], "project=sample summarize")

    def test_command_log_skips_denied_users(self):
        app = bridge.BridgeApp(replace(self.config, command_log_enabled=True))
        body, headers = self.signed_request(
            "project=sample summarize sensitive-placeholder-value",
            user_id="U_DENIED",
        )

        status, payload = app.handle_request(body, headers)

        self.assertEqual(status, 200)
        self.assertIn("User is not allowed", payload["text"])
        self.assertFalse(self.command_log_path.exists())

    def test_audit_log_migrates_old_header(self):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.write_text(
            "timestamp,user_id,channel_id,project,model,command_type,status\n"
            "2026-05-03T00:00:00+00:00,U1,C1,-,-,help,ok\n",
            encoding="utf-8",
        )

        app = bridge.BridgeApp(replace(self.config, command_log_enabled=True))
        body, headers = self.signed_request("help")
        app.handle_request(body, headers)

        audit = self.audit_path.read_text(encoding="utf-8")
        first_line = audit.splitlines()[0]
        self.assertEqual(
            first_line,
            "timestamp,user_id,channel_id,project,model,command_type,status,command_id",
        )
        self.assertIn("2026-05-03T00:00:00+00:00,U1,C1,-,-,help,ok,", audit)
        self.assertIn("help", self.command_log_path.read_text(encoding="utf-8"))

    def test_run_codex_prefers_last_message_file(self):
        def fake_run(args, **kwargs):
            prompt = args[-1]
            self.assertIn("summarize", prompt)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("final answer only", encoding="utf-8")
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="OpenAI Codex verbose transcript",
                stderr="",
            )

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                self.config.projects["sample"],
                "gpt-5.2-codex",
                "summarize",
            )

        self.assertEqual(result.status, "codex_ok")
        self.assertEqual(result.output, "final answer only")
        self.assertNotIn("verbose transcript", result.output)

    def test_run_codex_records_triggered_cli_and_model(self):
        captured_args = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("final answer only", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                self.config.projects["sample"],
                "gpt-5.2-codex",
                "summarize",
            )

        self.assertEqual(getattr(result, "cli_args", ()), tuple(captured_args))
        self.assertEqual(getattr(result, "actual_model", ""), "gpt-5.2-codex")

    def test_run_codex_persists_session_and_records_session_id(self):
        session_id = "11111111-1111-4111-8111-111111111111"

        def fake_run(args, **kwargs):
            self.assertIn("--json", args)
            self.assertNotIn("--ephemeral", args)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("final answer only", encoding="utf-8")
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {"type": "session_meta", "payload": {"id": session_id}}
                )
                + "\n",
                stderr="",
            )

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                self.config.projects["sample"],
                "gpt-5.2-codex",
                "summarize",
            )

        self.assertEqual(result.status, "codex_ok")
        self.assertEqual(result.output, "final answer only")
        self.assertEqual(result.session_id, session_id)

    def test_run_codex_resume_uses_existing_session_id(self):
        session_id = "22222222-2222-4222-8222-222222222222"

        def fake_run(args, **kwargs):
            self.assertIn("resume", args)
            self.assertIn("--json", args[args.index("resume") :])
            self.assertNotIn("--ephemeral", args)
            self.assertEqual(args[-2], session_id)
            self.assert_safety_wrapped_prompt(args[-1], task="follow up")
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("continued answer", encoding="utf-8")
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"session_id": session_id}) + "\n",
                stderr="",
            )

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                self.config.projects["sample"],
                "gpt-5.2-codex",
                "follow up",
                session_id=session_id,
            )

        self.assertEqual(result.status, "codex_ok")
        self.assertEqual(result.output, "continued answer")
        self.assertEqual(result.session_id, session_id)

    def test_app_mention_starts_thread_conversation(self):
        session_id = "33333333-3333-4333-8333-333333333333"

        class FakeWebClient:
            def __init__(self):
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)
                return {"ok": True, "ts": "100.2"}

        web_client = FakeWebClient()
        payload = {
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "text": "<@B1> summarize",
                "ts": "100.1",
            },
        }

        with patch.object(
            self.app,
            "run_codex",
            return_value=bridge.CodexRunResult(
                status="codex_ok",
                output="thread answer",
                session_id=session_id,
            ),
        ) as run_codex:
            status = self.app.handle_event(payload, web_client)

        self.assertEqual(status, "event_ok")
        run_codex.assert_called_once_with(
            self.config.projects["sample"],
            "gpt-5.2-codex",
            "summarize",
            session_id="",
        )
        record = self.app.conversations.get("T1", "C1", "100.1")
        self.assertIsNotNone(record)
        self.assertEqual(record.session_id, session_id)
        self.assertEqual(len(web_client.messages), 2)
        self.assertEqual(web_client.messages[0]["channel"], "C1")
        self.assertEqual(web_client.messages[0]["thread_ts"], "100.1")
        self.assertIn("Received", web_client.messages[0]["text"])
        self.assertIn("Running Codex", web_client.messages[0]["text"])
        self.assertEqual(web_client.messages[1]["channel"], "C1")
        self.assertEqual(web_client.messages[1]["thread_ts"], "100.1")
        self.assertIn("thread answer", web_client.messages[1]["text"])

    def test_app_mention_help_shows_mention_usage(self):
        config = self.multi_tool_config()
        app = bridge.BridgeApp(config)

        class FakeWebClient:
            def __init__(self):
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)
                return {"ok": True, "ts": "100.2"}

        web_client = FakeWebClient()
        payload = {
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "text": "<@B1> help",
                "ts": "100.1",
            },
        }

        with patch.object(app, "run_tool") as run_tool:
            status = app.handle_event(payload, web_client)

        self.assertEqual(status, "event_ok")
        run_tool.assert_not_called()
        self.assertEqual(len(web_client.messages), 1)
        text = web_client.messages[0]["text"]
        self.assertIn("Slack bot mention usage", text)
        self.assertIn("@bot claude project=<project> model=<model> <prompt>", text)
        self.assertIn("@bot copilot <prompt>", text)
        self.assertIn("Reply in the same Slack thread", text)
        self.assertNotIn("/claude project=<project>", text)

    def test_empty_user_allowlist_allows_any_event_user(self):
        app = bridge.BridgeApp(replace(self.config, allowed_users=frozenset()))

        class FakeWebClient:
            def __init__(self):
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)
                return {"ok": True, "ts": "100.2"}

        web_client = FakeWebClient()
        payload = {
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U2",
                "channel": "C1",
                "text": "<@B1> help",
                "ts": "100.1",
            },
        }

        status = app.handle_event(payload, web_client)

        self.assertEqual(status, "event_ok")
        self.assertEqual(len(web_client.messages), 1)
        self.assertIn("Slack bot mention usage", web_client.messages[0]["text"])

    def test_app_mention_can_start_claude_thread_conversation(self):
        session_id = "55555555-5555-4555-8555-555555555555"
        config = self.multi_tool_config()
        app = bridge.BridgeApp(config)

        class FakeWebClient:
            def __init__(self):
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)
                return {"ok": True, "ts": "100.2"}

        web_client = FakeWebClient()
        payload = {
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "text": "<@B1> claude summarize",
                "ts": "100.1",
            },
        }

        with patch.object(
            app,
            "run_claude",
            return_value=bridge.CodexRunResult(
                status="claude_ok",
                output="thread answer",
                session_id=session_id,
            ),
        ) as run_claude:
            status = app.handle_event(payload, web_client)

        self.assertEqual(status, "event_ok")
        run_claude.assert_called_once_with(
            config.tools["claude"],
            config.projects["sample"],
            "sonnet",
            "summarize",
            session_id="",
        )
        record = app.conversations.get("T1", "C1", "100.1")
        self.assertIsNotNone(record)
        self.assertEqual(record.tool_name, "claude")
        self.assertEqual(record.session_id, session_id)
        self.assertIn("Running Claude", web_client.messages[0]["text"])

    def test_thread_message_resumes_existing_conversation(self):
        session_id = "44444444-4444-4444-8444-444444444444"
        self.app.conversations.put(
            bridge.ConversationRecord(
                team_id="T1",
                channel_id="C1",
                thread_ts="100.1",
                session_id=session_id,
                tool_name="codex",
                project="sample",
                model="gpt-5.2-codex",
                project_explicit=False,
                model_explicit=False,
            )
        )

        class FakeWebClient:
            def __init__(self):
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)
                return {"ok": True, "ts": "100.3"}

        web_client = FakeWebClient()
        payload = {
            "team_id": "T1",
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "text": "follow up",
                "thread_ts": "100.1",
                "ts": "100.2",
            },
        }

        with patch.object(
            self.app,
            "run_codex",
            return_value=bridge.CodexRunResult(
                status="codex_ok",
                output="continued answer",
                session_id=session_id,
            ),
        ) as run_codex:
            status = self.app.handle_event(payload, web_client)

        self.assertEqual(status, "event_ok")
        run_codex.assert_called_once_with(
            self.config.projects["sample"],
            "gpt-5.2-codex",
            "follow up",
            session_id=session_id,
        )
        self.assertEqual(len(web_client.messages), 2)
        self.assertEqual(web_client.messages[0]["thread_ts"], "100.1")
        self.assertIn("Received", web_client.messages[0]["text"])
        self.assertIn("Running Codex", web_client.messages[0]["text"])
        self.assertEqual(web_client.messages[1]["thread_ts"], "100.1")
        self.assertIn("continued answer", web_client.messages[1]["text"])

    def test_thread_message_resumes_existing_copilot_conversation(self):
        session_id = "66666666-6666-4666-8666-666666666666"
        config = self.multi_tool_config()
        app = bridge.BridgeApp(config)
        app.conversations.put(
            bridge.ConversationRecord(
                team_id="T1",
                channel_id="C1",
                thread_ts="100.1",
                session_id=session_id,
                tool_name="copilot",
                project="sample",
                model="default",
                project_explicit=False,
                model_explicit=False,
            )
        )

        class FakeWebClient:
            def __init__(self):
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)
                return {"ok": True, "ts": "100.3"}

        web_client = FakeWebClient()
        payload = {
            "team_id": "T1",
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "text": "follow up",
                "thread_ts": "100.1",
                "ts": "100.2",
            },
        }

        with patch.object(
            app,
            "run_copilot",
            return_value=bridge.CodexRunResult(
                status="copilot_ok",
                output="continued answer",
                session_id=session_id,
            ),
        ) as run_copilot:
            status = app.handle_event(payload, web_client)

        self.assertEqual(status, "event_ok")
        run_copilot.assert_called_once_with(
            config.tools["copilot"],
            config.projects["sample"],
            "default",
            "follow up",
            session_id=session_id,
        )
        self.assertEqual(len(web_client.messages), 2)
        self.assertIn("Running Copilot", web_client.messages[0]["text"])
        self.assertIn("continued answer", web_client.messages[1]["text"])

    def test_thread_app_mention_strips_existing_tool_prefix(self):
        session_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        config = self.multi_tool_config()
        app = bridge.BridgeApp(config)
        app.conversations.put(
            bridge.ConversationRecord(
                team_id="T1",
                channel_id="C1",
                thread_ts="100.1",
                session_id=session_id,
                tool_name="claude",
                project="sample",
                model="sonnet",
                project_explicit=False,
                model_explicit=False,
            )
        )

        class FakeWebClient:
            def __init__(self):
                self.messages = []

            def chat_postMessage(self, **kwargs):
                self.messages.append(kwargs)
                return {"ok": True, "ts": "100.3"}

        payload = {
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "text": "<@B1> claude follow up",
                "thread_ts": "100.1",
                "ts": "100.2",
            },
        }

        with patch.object(
            app,
            "run_claude",
            return_value=bridge.CodexRunResult(
                status="claude_ok",
                output="continued answer",
                session_id=session_id,
            ),
        ) as run_claude:
            status = app.handle_event(payload, FakeWebClient())

        self.assertEqual(status, "event_ok")
        run_claude.assert_called_once_with(
            config.tools["claude"],
            config.projects["sample"],
            "sonnet",
            "follow up",
            session_id=session_id,
        )

    def test_top_level_message_without_thread_conversation_is_ignored(self):
        payload = {
            "team_id": "T1",
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "text": "not for codex",
                "ts": "100.1",
            },
        }

        with patch.object(self.app, "run_codex") as run_codex:
            status = self.app.handle_event(payload, object())

        self.assertEqual(status, "event_ignored")
        run_codex.assert_not_called()

    def test_execute_and_format_displays_explicit_project_and_model(self):
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="gpt-5.2-codex",
            prompt="summarize",
            project_explicit=True,
            model_explicit=True,
        )

        def fake_run(args, **kwargs):
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("final answer only", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            text, status = self.app.execute_and_format(
                self.config.projects["sample"],
                action,
            )

        self.assertEqual(status, "codex_ok")
        self.assertIn("project: sample", text)
        self.assertIn("model: gpt-5.2-codex", text)
        self.assertNotIn("cli:", text)
        self.assertNotIn("codex read-only result", text)
        self.assertNotIn("summarize", text)
        self.assertNotIn("--model", text)

    def test_execute_and_format_displays_default_for_implicit_project_and_model(self):
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="gpt-5.2-codex",
            prompt="summarize",
        )

        with patch.object(
            self.app,
            "run_codex",
            return_value=bridge.CodexRunResult(
                status="codex_ok",
                output="final answer only",
            ),
        ):
            text, status = self.app.execute_and_format(
                self.config.projects["sample"],
                action,
            )

        self.assertEqual(status, "codex_ok")
        self.assertEqual(
            text,
            "tool: codex\n"
            "project: default\n"
            "model: default\n"
            "status: codex_ok\n"
            "\n"
            "```\n"
            "final answer only\n"
            "```",
        )

    def test_execute_and_format_reports_cli_default_when_model_is_not_passed(self):
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="",
            prompt="summarize",
        )

        def fake_run(args, **kwargs):
            self.assertNotIn("--model", args)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("final answer only", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            text, status = self.app.execute_and_format(
                self.config.projects["sample"],
                action,
            )

        self.assertEqual(status, "codex_ok")
        self.assertIn("model: default", text)
        self.assertNotIn("cli:", text)
        self.assertNotIn('"--model"', text)

    def test_execute_and_format_preview_truncates_output(self):
        app = bridge.BridgeApp(
            replace(self.config, output_mode="preview", output_char_limit=20)
        )
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="gpt-5.2-codex",
            prompt="summarize",
        )

        with patch.object(
            app,
            "run_codex",
            return_value=bridge.CodexRunResult(
                status="codex_ok",
                output="abcdefghijklmnopqrstuvwxyz",
            ),
        ):
            text, status = app.execute_and_format(
                app.config.projects["sample"],
                action,
            )

        self.assertEqual(status, "codex_ok")
        self.assertIn("[truncated]", text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", text)

    def test_execute_and_format_full_does_not_truncate_output(self):
        app = bridge.BridgeApp(
            replace(self.config, output_mode="full", output_char_limit=20)
        )
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="gpt-5.2-codex",
            prompt="summarize",
        )

        with patch.object(
            app,
            "run_codex",
            return_value=bridge.CodexRunResult(
                status="codex_ok",
                output="abcdefghijklmnopqrstuvwxyz",
            ),
        ):
            text, status = app.execute_and_format(
                app.config.projects["sample"],
                action,
            )

        self.assertEqual(status, "codex_ok")
        self.assertNotIn("[truncated]", text)
        self.assertIn("abcdefghijklmnopqrstuvwxyz", text)

    def test_execute_and_format_none_omits_output_block(self):
        app = bridge.BridgeApp(replace(self.config, output_mode="none"))
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="gpt-5.2-codex",
            prompt="summarize",
        )

        with patch.object(
            app,
            "run_codex",
            return_value=bridge.CodexRunResult(
                status="codex_ok",
                output="hidden answer",
            ),
        ):
            text, status = app.execute_and_format(
                app.config.projects["sample"],
                action,
            )

        self.assertEqual(status, "codex_ok")
        self.assertIn("status: codex_ok", text)
        self.assertNotIn("hidden answer", text)
        self.assertNotIn("```", text)

    def test_public_summary_respects_output_mode_none(self):
        app = bridge.BridgeApp(replace(self.config, output_mode="none"))
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="gpt-5.2-codex",
            prompt="summarize",
            public=True,
        )
        result = bridge.CodexRunResult(
            status="codex_ok",
            output="sensitive answer",
        )

        summary = app.format_public_summary(
            app.config.projects["sample"],
            action,
            app.default_tool(),
            result,
        )

        self.assertIn("tool: codex", summary)
        self.assertNotIn("sensitive answer", summary)
        self.assertIn("Output display is disabled by config.", summary)

    def test_run_codex_wraps_user_prompt_with_safety_rules(self):
        def fake_run(args, **kwargs):
            prompt = args[-1]
            self.assert_safety_wrapped_prompt(prompt)
            self.assertNotIn("placeholder", prompt.lower())
            self.assertNotIn("already present", prompt.lower())
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("ok", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                self.config.projects["sample"],
                "gpt-5.2-codex",
                "summarize",
            )

        self.assertEqual(result.status, "codex_ok")

    def test_run_codex_safety_wrapper_keeps_user_task_plain(self):
        def fake_run(args, **kwargs):
            prompt = args[-1]
            self.assertNotIn("command text", prompt)
            self.assertNotIn("task body", prompt)
            self.assertNotIn("Slack user request", prompt)
            self.assertNotIn("Slack /codex command", prompt)
            self.assertNotIn("<task>", prompt)
            self.assertNotIn("</task>", prompt)
            self.assert_safety_wrapped_prompt(prompt)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("ok", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                self.config.projects["sample"],
                "gpt-5.2-codex",
                "summarize",
            )

        self.assertEqual(result.status, "codex_ok")

    def test_run_codex_project_file_access_ignores_user_config(self):
        def fake_run(args, **kwargs):
            self.assertIn("--ignore-user-config", args)
            self.assertNotIn("sandbox_permissions=[\"disk-full-read-access\"]", args)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("project-only", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                self.config.projects["sample"],
                "gpt-5.2-codex",
                "summarize",
            )

        self.assertEqual(result.status, "codex_ok")

    def test_run_codex_all_file_access_grants_full_disk_read(self):
        app = bridge.BridgeApp(replace(self.config, file_access="all"))

        def fake_run(args, **kwargs):
            self.assertIn("--ignore-user-config", args)
            self.assertIn("-c", args)
            self.assertIn("sandbox_permissions=[\"disk-full-read-access\"]", args)
            self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("all-files", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = app.run_codex(
                app.config.projects["sample"],
                "gpt-5.2-codex",
                "summarize",
            )

        self.assertEqual(result.status, "codex_ok")

    def test_run_codex_all_project_grants_full_disk_read(self):
        all_project = bridge.ProjectConfig(name="all", path=self.project)

        def fake_run(args, **kwargs):
            self.assert_safety_wrapped_prompt(args[-1], access_mode="ALL")
            self.assertIn("--ignore-user-config", args)
            self.assertIn("-c", args)
            self.assertIn("sandbox_permissions=[\"disk-full-read-access\"]", args)
            self.assertEqual(args[args.index("--cd") + 1], str(self.project))
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("all-project", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                all_project,
                "gpt-5.2-codex",
                "summarize",
            )

        self.assertEqual(result.status, "codex_ok")

    def test_run_codex_omits_model_and_reads_user_config_for_cli_default(self):
        def fake_run(args, **kwargs):
            self.assertNotIn("--ignore-user-config", args)
            self.assertNotIn("--model", args)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("cli-default", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_codex(
                self.config.projects["sample"],
                "",
                "summarize",
            )

        self.assertEqual(result.status, "codex_ok")
        self.assertEqual(result.output, "cli-default")

    def test_run_codex_resolves_windows_cmd_suffix(self):
        app = bridge.BridgeApp(replace(self.config, codex_command="codex"))

        def fake_which(command):
            if command == "codex":
                return r"C:\Users\runner\AppData\Roaming\npm\codex.CMD"
            if command == "codex.cmd":
                return r"C:\Users\runner\AppData\Roaming\npm\codex.cmd"
            return None

        def fake_run(args, **kwargs):
            self.assertEqual(args[0], "codex.cmd")
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("resolved", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("bridge.os.name", "nt"), patch("shutil.which", side_effect=fake_which):
            with patch("bridge.subprocess.run", side_effect=fake_run):
                result = app.run_codex(
                    app.config.projects["sample"],
                    "gpt-5.2-codex",
                    "summarize",
                )

        self.assertEqual(result.status, "codex_ok")
        self.assertEqual(result.output, "resolved")

    def test_run_claude_uses_noninteractive_read_only_args(self):
        tool = bridge.ToolConfig(
            name="claude",
            command="claude.cmd",
            default_model="sonnet",
            allowed_models=frozenset({"sonnet"}),
        )

        def fake_run(args, **kwargs):
            self.assertEqual(args[0], "claude.cmd")
            self.assertIn("--print", args)
            self.assertEqual(args[args.index("--model") + 1], "sonnet")
            self.assertEqual(args[args.index("--output-format") + 1], "json")
            self.assertNotIn("--no-session-persistence", args)
            self.assertNotIn("--resume", args)
            self.assertNotIn("--bare", args)
            self.assertIn("--disable-slash-commands", args)
            self.assertIn("--tools=Read,Grep,Glob,LS", args)
            self.assertIn(
                "--allowedTools=Read(/**),Grep(/**),Glob(/**),LS(/**)",
                args,
            )
            self.assertIn(
                "--disallowedTools=Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite",
                args,
            )
            self.assertNotIn("--tools", args)
            self.assertNotIn("--allowedTools", args)
            self.assertNotIn("--add-dir", args)
            self.assertNotIn("Edit,Write", args)
            self.assert_safety_wrapped_prompt(args[-1])
            self.assertEqual(kwargs["cwd"], str(self.project))
            return subprocess.CompletedProcess(args, 0, stdout="answer", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_tool(
                tool,
                self.config.projects["sample"],
                "sonnet",
                "summarize",
            )

        self.assertEqual(result.status, "claude_ok")
        self.assertEqual(result.output, "answer")

    def test_run_claude_all_project_uses_all_path_read_tools(self):
        tool = bridge.ToolConfig(
            name="claude",
            command="claude.cmd",
            default_model="sonnet",
            allowed_models=frozenset({"sonnet"}),
        )
        all_project = bridge.ProjectConfig(name="all", path=self.project)

        def fake_run(args, **kwargs):
            self.assertIn("--tools=Read,Grep,Glob,LS", args)
            self.assertIn(
                "--allowedTools=Read(/**),Grep(/**),Glob(/**),LS(/**)",
                args,
            )
            self.assertIn(
                "--disallowedTools=Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite",
                args,
            )
            self.assertEqual(kwargs["cwd"], str(self.project))
            return subprocess.CompletedProcess(args, 0, stdout="answer", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_tool(
                tool,
                all_project,
                "sonnet",
                "summarize",
            )

        self.assertEqual(result.status, "claude_ok")
        self.assertEqual(result.output, "answer")

    def test_run_claude_resolves_windows_cmd_suffix(self):
        tool = bridge.ToolConfig(
            name="claude",
            command="claude",
            default_model="sonnet",
            allowed_models=frozenset({"sonnet"}),
        )

        def fake_which(command):
            if command == "claude":
                return r"C:\Users\runner\AppData\Roaming\npm\claude.CMD"
            if command == "claude.cmd":
                return r"C:\Users\runner\AppData\Roaming\npm\claude.cmd"
            return None

        def fake_run(args, **kwargs):
            self.assertEqual(args[0], "claude.cmd")
            self.assertIn("--print", args)
            return subprocess.CompletedProcess(args, 0, stdout="answer", stderr="")

        with patch("bridge.os.name", "nt"), patch("shutil.which", side_effect=fake_which):
            with patch("bridge.subprocess.run", side_effect=fake_run):
                result = self.app.run_tool(
                    tool,
                    self.config.projects["sample"],
                    "sonnet",
                    "summarize",
                )

        self.assertEqual(result.status, "claude_ok")
        self.assertEqual(result.output, "answer")

    def test_run_claude_reports_windows_command_candidates_when_missing(self):
        tool = bridge.ToolConfig(
            name="claude",
            command="claude",
            default_model="sonnet",
            allowed_models=frozenset({"sonnet"}),
        )

        with patch("bridge.os.name", "nt"), patch("shutil.which", return_value=None):
            with patch("bridge.subprocess.run", side_effect=FileNotFoundError):
                result = self.app.run_tool(
                    tool,
                    self.config.projects["sample"],
                    "sonnet",
                    "summarize",
                )

        self.assertEqual(result.status, "claude_error")
        self.assertIn("claude command not found: claude", result.output)
        self.assertIn("Tried: claude, claude.cmd, claude.exe, claude.bat", result.output)

    def test_run_claude_omits_model_for_cli_default(self):
        tool = bridge.ToolConfig(
            name="claude",
            command="claude",
            default_model="",
            allowed_models=frozenset({"sonnet"}),
        )

        def fake_run(args, **kwargs):
            self.assertNotIn("--model", args)
            self.assert_safety_wrapped_prompt(args[-1])
            return subprocess.CompletedProcess(args, 0, stdout="answer", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_tool(
                tool,
                self.config.projects["sample"],
                "",
                "summarize",
            )

        self.assertEqual(result.status, "claude_ok")
        self.assertEqual(result.output, "answer")

    def test_run_claude_json_output_extracts_session_id(self):
        session_id = "77777777-7777-4777-8777-777777777777"
        tool = bridge.ToolConfig(
            name="claude",
            command="claude",
            default_model="sonnet",
            allowed_models=frozenset({"sonnet"}),
        )

        def fake_run(args, **kwargs):
            self.assertIn("--output-format", args)
            self.assertEqual(args[args.index("--output-format") + 1], "json")
            self.assertNotIn("--no-session-persistence", args)
            self.assertNotIn("--resume", args)
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {
                        "type": "result",
                        "result": "answer",
                        "session_id": session_id,
                    }
                ),
                stderr="",
            )

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_claude(
                tool,
                self.config.projects["sample"],
                "sonnet",
                "summarize",
            )

        self.assertEqual(result.status, "claude_ok")
        self.assertEqual(result.output, "answer")
        self.assertEqual(result.session_id, session_id)

    def test_run_claude_resume_uses_existing_session_id(self):
        session_id = "88888888-8888-4888-8888-888888888888"
        tool = bridge.ToolConfig(
            name="claude",
            command="claude",
            default_model="sonnet",
            allowed_models=frozenset({"sonnet"}),
        )

        def fake_run(args, **kwargs):
            self.assertIn("--resume", args)
            self.assertEqual(args[args.index("--resume") + 1], session_id)
            self.assert_safety_wrapped_prompt(args[-1], task="follow up")
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {
                        "type": "result",
                        "result": "continued answer",
                        "session_id": session_id,
                    }
                ),
                stderr="",
            )

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_claude(
                tool,
                self.config.projects["sample"],
                "sonnet",
                "follow up",
                session_id=session_id,
            )

        self.assertEqual(result.status, "claude_ok")
        self.assertEqual(result.output, "continued answer")
        self.assertEqual(result.session_id, session_id)

    def test_run_copilot_uses_noninteractive_read_only_args(self):
        tool = bridge.ToolConfig(
            name="copilot",
            command="copilot.cmd",
            default_model="default",
            allowed_models=frozenset({"default"}),
        )

        def fake_run(args, **kwargs):
            self.assertEqual(args[0], "copilot.cmd")
            self.assertIn("--prompt", args)
            self.assertEqual(args[args.index("--model") + 1], "default")
            self.assertEqual(args[args.index("--output-format") + 1], "json")
            self.assertNotIn("--silent", args)
            self.assertIn("--no-custom-instructions", args)
            self.assertIn("--excluded-tools=skill", args)
            self.assertIn("--no-ask-user", args)
            self.assertIn("--no-color", args)
            self.assertIn("--disable-builtin-mcps", args)
            self.assertIn("--disallow-temp-dir", args)
            self.assertIn("--available-tools=view,glob,grep", args)
            self.assertIn("--no-remote", args)
            self.assertNotIn("--deny-tool=write", args)
            self.assertNotIn("--deny-tool=shell", args)
            self.assertNotIn("--deny-tool=url", args)
            self.assertNotIn("--allow-all-paths", args)
            self.assertNotIn("--add-dir", args)
            self.assert_safety_wrapped_prompt(args[args.index("--prompt") + 1])
            self.assertEqual(kwargs["cwd"], str(self.project))
            return subprocess.CompletedProcess(args, 0, stdout="answer", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_tool(
                tool,
                self.config.projects["sample"],
                "default",
                "summarize",
            )

        self.assertEqual(result.status, "copilot_ok")
        self.assertEqual(result.output, "answer")

    def test_run_copilot_all_project_allows_all_paths(self):
        tool = bridge.ToolConfig(
            name="copilot",
            command="copilot.cmd",
            default_model="default",
            allowed_models=frozenset({"default"}),
        )
        all_project = bridge.ProjectConfig(name="all", path=self.project)

        def fake_run(args, **kwargs):
            self.assertIn("--allow-all-paths", args)
            self.assertIn("--disallow-temp-dir", args)
            self.assert_safety_wrapped_prompt(
                args[args.index("--prompt") + 1],
                access_mode="ALL",
            )
            self.assertEqual(kwargs["cwd"], str(self.project))
            return subprocess.CompletedProcess(args, 0, stdout="answer", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_tool(
                tool,
                all_project,
                "default",
                "summarize",
            )

        self.assertEqual(result.status, "copilot_ok")
        self.assertEqual(result.output, "answer")

    def test_run_copilot_omits_model_for_cli_default(self):
        tool = bridge.ToolConfig(
            name="copilot",
            command="copilot",
            default_model="",
            allowed_models=frozenset({"default"}),
        )

        def fake_run(args, **kwargs):
            self.assertNotIn("--model", args)
            self.assert_safety_wrapped_prompt(args[args.index("--prompt") + 1])
            return subprocess.CompletedProcess(args, 0, stdout="answer", stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_tool(
                tool,
                self.config.projects["sample"],
                "",
                "summarize",
            )

        self.assertEqual(result.status, "copilot_ok")
        self.assertEqual(result.output, "answer")

    def test_run_copilot_jsonl_output_extracts_session_id(self):
        session_id = "99999999-9999-4999-8999-999999999999"
        tool = bridge.ToolConfig(
            name="copilot",
            command="copilot",
            default_model="default",
            allowed_models=frozenset({"default"}),
        )

        def fake_run(args, **kwargs):
            self.assertIn("--output-format", args)
            self.assertEqual(args[args.index("--output-format") + 1], "json")
            self.assertIn("--no-custom-instructions", args)
            self.assertIn("--excluded-tools=skill", args)
            self.assertIn("--no-ask-user", args)
            self.assertNotIn("--silent", args)
            stdout = "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "data": {"content": "answer"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "result",
                            "sessionId": session_id,
                            "exitCode": 0,
                        }
                    ),
                ]
            )
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_copilot(
                tool,
                self.config.projects["sample"],
                "default",
                "summarize",
            )

        self.assertEqual(result.status, "copilot_ok")
        self.assertEqual(result.output, "answer")
        self.assertEqual(result.session_id, session_id)

    def test_run_copilot_resume_uses_existing_session_id(self):
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        tool = bridge.ToolConfig(
            name="copilot",
            command="copilot",
            default_model="default",
            allowed_models=frozenset({"default"}),
        )

        def fake_run(args, **kwargs):
            resume_args = [arg for arg in args if arg.startswith("--resume=")]
            self.assertEqual(resume_args, [f"--resume={session_id}"])
            self.assert_safety_wrapped_prompt(
                args[args.index("--prompt") + 1],
                task="follow up",
            )
            stdout = "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "data": {"content": "continued answer"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "result",
                            "sessionId": session_id,
                            "exitCode": 0,
                        }
                    ),
                ]
            )
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with patch("bridge.subprocess.run", side_effect=fake_run):
            result = self.app.run_copilot(
                tool,
                self.config.projects["sample"],
                "default",
                "follow up",
                session_id=session_id,
            )

        self.assertEqual(result.status, "copilot_ok")
        self.assertEqual(result.output, "continued answer")
        self.assertEqual(result.session_id, session_id)

    def test_run_and_post_replaces_running_message(self):
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="gpt-5.2-codex",
            prompt="summarize",
        )

        with patch.object(
            self.app,
            "run_tool",
            return_value=bridge.CodexRunResult(status="codex_ok", output="final text"),
        ), patch("bridge.post_to_response_url", return_value=True) as post:
            self.app.run_and_post(
                "https://hooks.slack.com/commands/response",
                self.config.projects["sample"],
                action,
                "U1",
                "C1",
            )

        expected_text = (
            "tool: codex\n"
            "project: default\n"
            "model: default\n"
            "status: codex_ok\n"
            "\n"
            "```\n"
            "final text\n"
            "```"
        )
        post.assert_called_once_with(
            "https://hooks.slack.com/commands/response",
            expected_text,
            replace_original=True,
        )

    def test_run_and_post_public_sends_in_channel_summary_after_private_result(self):
        action = bridge.ParsedAction(
            command_type="run",
            project="sample",
            model="gpt-5.2-codex",
            prompt="summarize",
            public=True,
        )

        with patch.object(
            self.app,
            "run_tool",
            return_value=bridge.CodexRunResult(
                status="codex_ok",
                output="short public answer",
                cli_args=("codex.cmd", "exec", "--sandbox", "read-only"),
                actual_model="gpt-5.2-codex",
            ),
        ), patch("bridge.post_to_response_url", return_value=True) as post:
            self.app.run_and_post(
                "https://hooks.slack.com/commands/response",
                self.config.projects["sample"],
                action,
                "U1",
                "C1",
            )

        self.assertEqual(post.call_count, 2)
        private_call = post.call_args_list[0]
        public_call = post.call_args_list[1]
        self.assertEqual(private_call.args[1].splitlines()[0], "tool: codex")
        self.assertEqual(private_call.kwargs, {"replace_original": True})
        self.assertIn("tool: codex", public_call.args[1])
        self.assertIn("short public answer", public_call.args[1])
        self.assertNotIn("cli:", public_call.args[1])
        self.assertEqual(
            public_call.kwargs,
            {"response_type": "in_channel"},
        )


if __name__ == "__main__":
    unittest.main()



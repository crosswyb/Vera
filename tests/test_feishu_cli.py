import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from live.feishu_cli import FeishuCLI, FeishuError, folder_token, official_url


class FeishuAdapterTests(unittest.TestCase):
    def setUp(self):
        self.cli = FeishuCLI()

    def test_folder_links_are_limited_to_official_folder_pages(self):
        self.assertEqual(folder_token("https://demo.feishu.cn/drive/folder/Abcd1234?from=copy"), "Abcd1234")
        for url in (
            "https://demo.feishu.cn/docx/Abcd1234",
            "https://feishu.cn.example.org/drive/folder/Abcd1234",
            "https://name@feishu.cn/drive/folder/Abcd1234",
            "file:///drive/folder/Abcd1234",
        ):
            with self.subTest(url=url), self.assertRaises(FeishuError):
                folder_token(url)
        self.assertFalse(official_url("https://feishu.cn:8443/drive/folder/Abcd1234"))

    def test_metadata_and_chat_contracts(self):
        with patch.object(
            self.cli,
            "_run",
            side_effect=[
                {"data": {"metas": [{"doc_type": "folder", "doc_token": "Folder987", "title": "合成资料"}]}},
                {
                    "data": {
                        "chats": [
                            {"chat_id": "oc_sync11", "name": "试用"},
                            {"chat_id": "oc_gone11", "name": "旧群", "chat_status": "dissolved_save"},
                        ]
                    }
                },
                {"data": {"name": "试用", "chat_status": "normal"}},
            ],
        ) as run:
            self.assertEqual(self.cli.validate_folder("Folder987"), {"token": "Folder987", "name": "合成资料"})
            self.assertEqual(self.cli.search_chats("试用"), [{"id": "oc_sync11", "name": "试用"}])
            self.assertEqual(self.cli.validate_chat("oc_sync11"), {"id": "oc_sync11", "name": "试用"})
        for call in run.call_args_list:
            self.assertIn("--as", call.args[0])
            self.assertIn("user", call.args[0])

    def test_verified_user_account_binding(self):
        def check(brand, app, oid, verified):
            response = {
                "brand": brand,
                "appId": app,
                "identities": {
                    "bot": {"available": True, "verified": True},
                    "user": {"available": True, "verified": verified, "openId": oid, "userName": "张三"},
                },
            }
            with patch.object(self.cli, "_run", side_effect=["lark-cli version 1.0.100", response]):
                return self.cli.status()

        state = check("feishu", "app", "ou_one", True)
        self.assertEqual(state["account_name"], "张三")
        self.assertTrue(state["connected"])
        self.assertFalse(check("feishu", "app", "ou_one", False)["connected"])
        for args in [
            ("feishu", "other", "ou_one", True),
            ("lark", "app", "ou_one", True),
            ("feishu", "app", "ou_two", True),
        ]:
            self.assertNotEqual(state["account_key"], check(*args)["account_key"])

    def test_auth_pending_reuses_flow_and_keeps_device_code_in_backend(self):
        gate = threading.Event()
        finished = threading.Event()
        scopes = []

        def run(args, **_kwargs):
            if args == ["--version"]:
                return "lark-cli version 1.0.100"
            if "--no-wait" in args:
                scopes.extend(args[args.index("--scope") + 1].split())
                return {
                    "verification_url": "https://accounts.feishu.cn/device",
                    "device_code": "sensitive-device-code",
                    "expires_in": 60,
                }
            self.assertEqual(args[3], "sensitive-device-code")
            gate.wait(2)
            return {"ok": True}

        original = self.cli._finish_auth

        def finish(*args):
            try:
                original(*args)
            finally:
                finished.set()

        with (
            patch.object(self.cli, "_finish_auth", side_effect=finish),
            patch.object(self.cli, "_run", side_effect=run),
            patch.object(self.cli, "status", return_value={"connected": True, "message": "成功"}),
        ):
            for state in [self.cli.auth_start(), self.cli.auth_start(), self.cli.auth_poll()]:
                self.assertNotIn("sensitive-device-code", str(state))
            self.assertEqual(scopes.count("docx:document:create"), 1)
            self.assertIn("im:message", scopes)
            self.assertIn("im:message.send_as_user", scopes)
            gate.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(self.cli.auth_poll()["state"], "authorized")

    def test_process_runs_in_empty_directory_without_credentials_and_reads_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "lark-cli"
            binary.write_text(
                "#!"
                + sys.executable
                + "\n"
                + 'import os,sys,json\nprint(json.dumps({"files":os.listdir(),"args":sys.argv[1:],"body":sys.stdin.read(),'
                '"secret":os.getenv("MODEL_API_KEY"),"path":os.environ["PATH"]}))\n'
            )
            binary.chmod(0o700)
            with (
                patch("live.feishu_cli._native_binary", return_value=binary),
                patch.dict(os.environ, MODEL_API_KEY="fixture-do-not-inherit"),
            ):
                result = self.cli._run(["--title=问题 $(whoami)"], content="**合成资料**")
            self.assertEqual(
                result,
                {
                    "files": [],
                    "args": ["--title=问题 $(whoami)"],
                    "body": "**合成资料**",
                    "secret": None,
                    "path": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )

    def test_document_and_message_receipts_and_markdown(self):
        doc = {"document_id": "SyntheticDoc", "url": "https://demo.feishu.cn/docx/SyntheticDoc"}
        body = '> 原文\n[来源](https://example.org?a=1&b=2)\n![图](local.png)\n<img src="other.png">'
        with patch.object(
            self.cli, "_run", side_effect=[{"data": {"document": doc}}, {"data": {"message_id": "om_test"}}]
        ) as run:
            self.assertEqual(self.cli.create_document("Folder987", "问题", body), doc)
            self.assertEqual(
                self.cli.send_message("oc_test", "结论\n" + doc["url"], "sync-test"), {"message_id": "om_test"}
            )
        content = run.call_args_list[0].kwargs["content"]
        self.assertIn("> 原文", content)
        self.assertIn("https://example.org?a=1&b=2", content)
        self.assertNotIn("![", content)
        self.assertNotIn("<img", content)
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["--idempotency-key", "sync-test"])
        with patch.object(self.cli, "_run", return_value={"data": {"document": {"document_id": "SyntheticDoc"}}}):
            with self.assertRaises(FeishuError) as ctx:
                self.cli.create_document("Folder987", "问题", body)
        self.assertTrue(ctx.exception.uncertain)

    @unittest.skipUnless(
        os.environ.get("ZSJH_VERIFY_LARK_DRY_RUN") == "1", "requires installed and initialized lark-cli"
    )
    def test_real_cli_dry_run_confirms_exact_requests(self):
        # Exercise the production preparation path, intercepting only the remote receipt.
        real = self.cli._run
        plans = []

        def preview(args, **kwargs):
            result = real([*args, "--dry-run"], **kwargs)
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["identity"], "user")
            plans.append(result["data"]["api"])
            return {
                "data": {
                    "document": {"url": "https://sample.feishu.cn/docx/SyntheticDoc", "document_id": "SyntheticDoc"},
                    "message_id": "om_test",
                }
            }

        with patch.object(self.cli, "_run", side_effect=preview):
            doc = self.cli.create_document(
                "Folder987",
                "合成问题",
                '> 模拟原文\n[来源](https://example.org/?a=1&b=2)\n![图](local.png)\n<img src="https://example.org/file.png">',
            )
            self.cli.send_message("oc_synthetic", "合成摘要\n" + doc["url"], "synthetic-test")
        create, notification = plans
        self.assertEqual(
            [(a["method"], a["url"]) for a in create],
            [("POST", "/open-apis/docs_ai/v1/documents"), ("GET", "/open-apis/docs_ai/v1/async_tasks/<task_id>")],
        )
        self.assertEqual(create[0]["body"]["parent_token"], "Folder987")
        self.assertIn("> 模拟原文", create[0]["body"]["content"])
        self.assertIn("https://example.org/?a=1&b=2", create[0]["body"]["content"])
        self.assertEqual(len(notification), 1)
        target = notification[0]
        self.assertEqual((target["method"], target["url"]), ("POST", "/open-apis/im/v1/messages"))
        self.assertEqual(target["body"]["receive_id"], "oc_synthetic")
        self.assertEqual(target["body"]["uuid"], "synthetic-test")
        self.assertEqual(json.loads(target["body"]["content"])["text"], "合成摘要\n" + doc["url"])

"""邮件发送（qq_mail）步骤的测试。

覆盖：类型注册/默认参数/摘要/序列化（含多附件列表）、mail_actor（假 SMTP 全链路：
成功发送、认证失败、附件缺失、端口 fallback）、run_qq_mail_step（$变量名 解析、
必填校验、收件人去重、附件透传、停止）、参数对话框构建/回填/校验（必填、邮箱格式、
附件存在、授权码显隐、附件去重）。
真实网络/邮件发送在测试里完全规避（假 SMTP 替换 smtplib）。
"""
from __future__ import annotations

import os
import smtplib
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import FLOW_STEP_TYPES, Flow, FlowStep, default_step_params, \
    flow_from_dict, flow_to_dict


class TestStepMetadata(unittest.TestCase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("qq_mail"), "邮件发送")

    def test_old_display_name_migrated(self):
        """旧流程文件残留的「QQ邮件发送」名称加载时自动刷新为「邮件发送」。"""
        s = FlowStep(type="qq_mail", name="QQ邮件发送")
        self.assertEqual(s.name, "邮件发送")

    def test_custom_name_preserved(self):
        """用户自定义名称不受迁移影响。"""
        s = FlowStep(type="qq_mail", name="发测试邮件")
        self.assertEqual(s.name, "发测试邮件")

    def test_default_params(self):
        p = default_step_params("qq_mail")
        self.assertEqual(p["mail_host"], "smtp.qq.com")
        self.assertEqual(p["mail_port"], 465)
        self.assertEqual(p["mail_user"], "")
        self.assertEqual(p["mail_auth_code"], "")
        self.assertEqual(p["mail_to"], "")
        self.assertEqual(p["subject"], "")
        self.assertEqual(p["content"], "")
        self.assertEqual(p["attachments"], [])


class TestSummary(unittest.TestCase):
    def _summary(self, **params):
        s = FlowStep(type="qq_mail")
        s.params.update(params)
        return s.summary()

    def test_shows_recipient(self):
        self.assertIn("发邮件给", self._summary(mail_to="a@qq.com"))
        self.assertIn("a@qq.com", self._summary(mail_to="a@qq.com"))

    def test_empty_recipient(self):
        self.assertIn("未填收件人", self._summary(mail_to="   "))

    def test_attachment_count(self):
        s = self._summary(mail_to="a@qq.com",
                          attachments=["/a.txt", "/b.txt"])
        self.assertIn("2 附件", s)

    def test_no_attachment_mark(self):
        s = self._summary(mail_to="a@qq.com", attachments=[])
        self.assertNotIn("附件", s)


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="发信流程", steps=[FlowStep(type="qq_mail", name="发邮件", params={
            "mail_host": "smtp.qq.com", "mail_port": 465,
            "mail_user": "me@qq.com", "mail_auth_code": "xxxx",
            "mail_to": "you@qq.com;he@qq.com",
            "subject": "结果 $name", "content": "正文",
            "attachments": ["C:/a.txt", "C:/b.txt"],
        })])
        back = flow_from_dict(flow_to_dict(f))
        p = back.steps[0].params
        self.assertEqual(p["mail_host"], "smtp.qq.com")
        self.assertEqual(p["mail_port"], 465)
        self.assertEqual(p["attachments"], ["C:/a.txt", "C:/b.txt"])


# ---------- mail_actor（假 SMTP 全链路） ----------

class _FakeSMTP:
    def __init__(self, *a, **kw):
        self.logged_in = None
        self.sent = []
        self.quit_called = False

    def login(self, user, auth):
        self.logged_in = (user, auth)

    def sendmail(self, from_, to, msg):
        self.sent.append((from_, to, msg))

    def quit(self):
        self.quit_called = True

    # 非 465 端口走 STARTTLS 时用到
    def ehlo(self):
        pass

    def starttls(self):
        pass


class _AuthFailSMTP(_FakeSMTP):
    def login(self, user, auth):
        raise smtplib.SMTPAuthenticationError(535, b"auth failed")


class TestMailActor(unittest.TestCase):
    def _send(self, **kw):
        from app.mail_actor import send_mail
        base = dict(host="smtp.qq.com", port=465, user="me@qq.com",
                    auth_code="code", to_addrs=["you@qq.com"],
                    subject="主题", content="正文", attachments=[])
        base.update(kw)
        return send_mail(**base)

    def test_success_sends_via_ssl(self):
        fake = _FakeSMTP()
        with mock.patch("app.mail_actor.smtplib.SMTP_SSL", return_value=fake) as ssl:
            ok, why = self._send()
        self.assertTrue(ok, why)
        ssl.assert_called_once()
        self.assertEqual(fake.logged_in, ("me@qq.com", "code"))
        self.assertEqual(fake.sent[0][1], ["you@qq.com"])
        self.assertTrue(fake.quit_called)

    def test_auth_failure_reported(self):
        fake = _AuthFailSMTP()
        with mock.patch("app.mail_actor.smtplib.SMTP_SSL", return_value=fake):
            ok, why = self._send()
        self.assertFalse(ok)
        self.assertIn("认证失败", why)

    def test_missing_attachment_fails(self):
        ok, why = self._send(attachments=["/no/such/file.txt"])
        self.assertFalse(ok)
        self.assertIn("附件不存在", why)

    def test_non_465_uses_starttls(self):
        fake = _FakeSMTP()
        with mock.patch("app.mail_actor.smtplib.SMTP", return_value=fake) as smtp, \
             mock.patch("app.mail_actor.smtplib.SMTP_SSL") as ssl:
            ok, why = self._send(port=587)
        self.assertTrue(ok, why)
        smtp.assert_called_once()
        ssl.assert_not_called()

    def test_empty_user_fails(self):
        ok, why = self._send(user="")
        self.assertFalse(ok)
        self.assertIn("发送人邮箱", why)

    def test_empty_recipient_fails(self):
        ok, why = self._send(to_addrs=[])
        self.assertFalse(ok)
        self.assertIn("收件人", why)

    def test_attachment_included_in_mime(self):
        """附件内容以 base64 进入邮件体（验证 MIME 组装）。"""
        import tempfile
        with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as f:
            f.write(b"hello attachment")
            tmp = f.name
        try:
            fake = _FakeSMTP()
            with mock.patch("app.mail_actor.smtplib.SMTP_SSL", return_value=fake):
                ok, why = self._send(attachments=[tmp])
            self.assertTrue(ok, why)
            msg = fake.sent[0][2]
            # 附件内容以 base64 形式进入邮件体（编码后不含原文）
            import base64
            self.assertIn(base64.b64encode(b"hello attachment").decode(), msg)
            self.assertIn("Content-Disposition", msg)
        finally:
            os.unlink(tmp)


# ---------- run_qq_mail_step ----------

class TestRunQQMailStep(unittest.TestCase):
    def test_resolves_subject_and_content(self):
        from app.tasks import run_qq_mail_step
        with mock.patch("app.mail_actor.send_mail",
                        return_value=(True, "邮件已发送给 1 个收件人")) as sm:
            ok, _ = run_qq_mail_step(
                {"mail_user": "me@qq.com", "mail_auth_code": "c",
                 "mail_to": "you@qq.com", "subject": "$name 的邮件",
                 "content": "你好 $name", "attachments": []},
                {"name": "张三"})
        self.assertTrue(ok)
        kw = sm.call_args.kwargs
        self.assertEqual(kw["subject"], "张三 的邮件")
        self.assertEqual(kw["content"], "你好 张三")

    def test_resolves_recipient_and_dedups(self):
        from app.tasks import run_qq_mail_step
        with mock.patch("app.mail_actor.send_mail",
                        return_value=(True, "ok")) as sm:
            run_qq_mail_step(
                {"mail_user": "me@qq.com", "mail_auth_code": "c",
                 "mail_to": "$to", "subject": "s", "content": "b",
                 "attachments": []},
                {"to": "a@qq.com;b@qq.com, a@qq.com"})
        self.assertEqual(sm.call_args.kwargs["to_addrs"],
                         ["a@qq.com", "b@qq.com"])

    def test_passes_attachments(self):
        from app.tasks import run_qq_mail_step
        with mock.patch("app.mail_actor.send_mail",
                        return_value=(True, "ok")) as sm:
            run_qq_mail_step(
                {"mail_user": "me@qq.com", "mail_auth_code": "c",
                 "mail_to": "you@qq.com", "subject": "s", "content": "b",
                 "attachments": ["C:/a.txt", "", "C:/b.txt"]}, {})
        self.assertEqual(sm.call_args.kwargs["attachments"],
                         ["C:/a.txt", "C:/b.txt"])

    def test_missing_user_fails(self):
        from app.tasks import run_qq_mail_step
        with mock.patch("app.mail_actor.send_mail") as sm:
            ok, why = run_qq_mail_step(
                {"mail_user": "", "mail_auth_code": "c", "mail_to": "a@qq.com"},
                {})
        self.assertFalse(ok)
        self.assertIn("发送人邮箱未填写", why)
        sm.assert_not_called()

    def test_missing_auth_fails(self):
        from app.tasks import run_qq_mail_step
        with mock.patch("app.mail_actor.send_mail") as sm:
            ok, why = run_qq_mail_step(
                {"mail_user": "me@qq.com", "mail_auth_code": "", "mail_to": "a@qq.com"},
                {})
        self.assertFalse(ok)
        self.assertIn("授权码", why)
        sm.assert_not_called()

    def test_missing_to_fails(self):
        from app.tasks import run_qq_mail_step
        with mock.patch("app.mail_actor.send_mail") as sm:
            ok, why = run_qq_mail_step(
                {"mail_user": "me@qq.com", "mail_auth_code": "c", "mail_to": "  "},
                {})
        self.assertFalse(ok)
        self.assertIn("收件人邮箱未填写", why)
        sm.assert_not_called()

    def test_default_host_port(self):
        from app.tasks import run_qq_mail_step
        with mock.patch("app.mail_actor.send_mail",
                        return_value=(True, "ok")) as sm:
            run_qq_mail_step(
                {"mail_user": "me@qq.com", "mail_auth_code": "c",
                 "mail_to": "a@qq.com", "subject": "s", "content": "b"},
                {})
        self.assertEqual(sm.call_args.kwargs["host"], "smtp.qq.com")
        self.assertEqual(sm.call_args.kwargs["port"], 465)

    def test_stopped(self):
        import threading
        from app.tasks import run_qq_mail_step
        stop = threading.Event()
        stop.set()
        with mock.patch("app.mail_actor.send_mail") as sm:
            ok, why = run_qq_mail_step({"mail_user": "me@qq.com"}, {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")
        sm.assert_not_called()


# ---------- 对话框 ----------

class TestQQMailDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="qq_mail", params=params))

    def test_defaults(self):
        dlg = self._open({})
        self.assertEqual(dlg.mail_host.text(), "smtp.qq.com")
        self.assertEqual(dlg.mail_port.value(), 465)
        self.assertEqual(dlg.mail_user.text(), "")

    def test_auth_masked_by_default(self):
        from PySide6.QtWidgets import QLineEdit
        dlg = self._open({})
        self.assertEqual(dlg.mail_auth.echoMode(), QLineEdit.Password)

    def test_auth_show_toggle(self):
        from PySide6.QtWidgets import QLineEdit
        dlg = self._open({})
        dlg.mail_auth_show.setChecked(True)
        self.assertEqual(dlg.mail_auth.echoMode(), QLineEdit.Normal)

    def test_apply_roundtrip(self):
        dlg = self._open({})
        dlg.mail_user.setText("me@qq.com")
        dlg.mail_auth.setText("abcd")
        dlg.mail_to.setText("you@qq.com")
        dlg.mail_subject.setText("主题 $name")
        dlg.mail_content.setPlainText("正文")
        dlg._add_mail_attachment("C:/a.txt")
        dlg._add_mail_attachment("C:/b.txt")
        step = FlowStep(type="qq_mail")
        dlg.apply_to(step)
        p = step.params
        self.assertEqual(p["mail_host"], "smtp.qq.com")
        self.assertEqual(p["mail_port"], 465)
        self.assertEqual(p["mail_user"], "me@qq.com")
        self.assertEqual(p["mail_auth_code"], "abcd")
        self.assertEqual(p["mail_to"], "you@qq.com")
        self.assertEqual(p["subject"], "主题 $name")
        self.assertEqual(p["content"], "正文")
        self.assertEqual(p["attachments"], ["C:/a.txt", "C:/b.txt"])

    def test_attachment_dedup(self):
        dlg = self._open({})
        dlg._add_mail_attachment("C:/a.txt")
        dlg._add_mail_attachment("C:/a.txt")
        step = FlowStep(type="qq_mail")
        dlg.apply_to(step)
        self.assertEqual(step.params["attachments"], ["C:/a.txt"])

    def test_fill_restores_attachments(self):
        dlg = self._open({"attachments": ["C:/x.txt", "C:/y.txt"]})
        self.assertEqual(dlg.mail_attach_list.count(), 2)
        step = FlowStep(type="qq_mail")
        dlg.apply_to(step)
        self.assertEqual(step.params["attachments"], ["C:/x.txt", "C:/y.txt"])

    def test_accept_requires_user(self):
        dlg = self._open({})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertIn("发送人邮箱", warn.call_args.args[1])

    def test_accept_rejects_bad_user_format(self):
        dlg = self._open({"mail_user": "not-an-email"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertIn("格式不正确", warn.call_args.args[1])

    def test_accept_rejects_bad_recipient_format(self):
        dlg = self._open({"mail_user": "me@qq.com", "mail_auth_code": "c",
                          "mail_to": "good@qq.com; bad"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertIn("收件人邮箱格式", warn.call_args.args[1])

    def test_accept_rejects_missing_attachment(self):
        dlg = self._open({"mail_user": "me@qq.com", "mail_auth_code": "c",
                          "mail_to": "you@qq.com"})
        dlg._add_mail_attachment("C:/no/such/file.txt")
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertIn("附件不存在", warn.call_args.args[1])

    def test_accept_passes_when_valid(self):
        dlg = self._open({"mail_user": "me@qq.com", "mail_auth_code": "c",
                          "mail_to": "you@qq.com", "subject": "s", "content": "b"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_not_called()

    def test_insert_var_disabled_when_no_flow_vars(self):
        dlg = self._open({})
        self.assertFalse(dlg.mail_subject_var.isEnabled())
        self.assertIn("暂无变量", dlg.mail_subject_var.currentText())


if __name__ == "__main__":
    unittest.main()

"""DingTalk Phase 3 inbound media tests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sophagent.im.platforms.dingtalk.adapter import DingTalkAdapter, DingTalkConfig
from sophagent.im.platforms.dingtalk.media import (
    assert_download_url,
    collect_media_items,
    download_inbound_attachments,
    fetch_download_url,
    message_has_media,
    sanitize_download_url,
)


class TestMediaCollect:
    def test_collect_image_url(self):
        msg = SimpleNamespace(
            image_content=SimpleNamespace(
                download_code="https://down.dingtalk.com/file/abc.jpg",
            ),
        )
        items = collect_media_items(msg)
        assert len(items) == 1
        assert items[0].kind == "image"

    def test_collect_download_code_before_resolve(self):
        msg = SimpleNamespace(
            rich_text_content=SimpleNamespace(
                rich_text_list=[
                    {"type": "voice", "downloadCode": "opaque-code-123"},
                ],
            ),
        )
        items = collect_media_items(msg, resolved_only=False)
        assert len(items) == 1
        assert items[0].kind == "audio"

    def test_message_has_media_with_code(self):
        msg = SimpleNamespace(
            rich_text_content=SimpleNamespace(
                rich_text_list=[{"type": "file", "downloadCode": "pdf-code", "fileName": "a.pdf"}],
            ),
        )
        assert message_has_media(msg) is True

    def test_collect_standalone_file_message(self):
        from dingtalk_stream import ChatbotMessage

        msg = ChatbotMessage.from_dict({
            "msgtype": "file",
            "content": {"downloadCode": "file-code", "fileName": "resume.pdf"},
            "conversationId": "c1",
            "msgId": "m1",
            "robotCode": "app-key",
        })
        items = collect_media_items(msg, resolved_only=False)
        assert len(items) == 1
        assert items[0].kind == "file"
        assert items[0].name == "resume.pdf"
        assert message_has_media(msg) is True

    def test_collect_standalone_voice_message(self):
        from dingtalk_stream import ChatbotMessage

        msg = ChatbotMessage.from_dict({
            "msgtype": "voice",
            "content": {"downloadCode": "voice-code"},
            "conversationId": "c1",
            "msgId": "m1",
            "robotCode": "app-key",
        })
        items = collect_media_items(msg, resolved_only=False)
        assert len(items) == 1
        assert items[0].kind == "audio"


class TestDownloadUrlAllowlist:
    def test_allow_dingtalk_cdn(self):
        assert_download_url("https://down.dingtalk.com/c2c/abc")

    def test_upgrade_http_dingtalk_cdn(self):
        url = sanitize_download_url("http://down.dingtalk.com/c2c/abc.jpg")
        assert url == "https://down.dingtalk.com/c2c/abc.jpg"

    def test_reject_unknown_host(self):
        with pytest.raises(ValueError):
            assert_download_url("https://evil.example.com/x")


@pytest.mark.asyncio
async def test_fetch_download_url():
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"downloadUrl": "https://down.dingtalk.com/file/abc.jpg"}

    http = MagicMock()
    http.post = AsyncMock(return_value=FakeResp())
    url = await fetch_download_url(
        http,
        "opaque-code",
        access_token="tok",
        client_id="app-key",
    )
    assert url == "https://down.dingtalk.com/file/abc.jpg"


@pytest.mark.asyncio
async def test_download_inbound_attachments(tmp_path):
    message = SimpleNamespace(
        robot_code="app-key",
        image_content=SimpleNamespace(download_code="opaque-code"),
    )

    class FakeResponse:
        status_code = 200
        content = b"fake-image-bytes"
        headers = {"content-type": "image/jpeg"}

        def raise_for_status(self):
            return None

    http = MagicMock()
    http.post = AsyncMock(return_value=type("R", (), {
        "status_code": 200,
        "raise_for_status": lambda self: None,
        "json": lambda self: {"downloadUrl": "https://down.dingtalk.com/file/abc.jpg"},
    })())
    http.get = AsyncMock(return_value=FakeResponse())

    attachments = await download_inbound_attachments(
        http,
        message,
        inbound_dir=tmp_path / "inbound",
        access_token="tok",
        client_id="app-key",
    )
    assert len(attachments) == 1
    assert attachments[0].kind == "image"
    assert Path(attachments[0].path).exists()


@pytest.mark.asyncio
async def test_download_inbound_attachments_http_url(tmp_path):
    message = SimpleNamespace(
        robot_code="app-key",
        image_content=SimpleNamespace(download_code="opaque-code"),
    )

    class FakeResponse:
        status_code = 200
        content = b"fake-image-bytes"
        headers = {"content-type": "image/jpeg"}

        def raise_for_status(self):
            return None

    http = MagicMock()
    http.post = AsyncMock(return_value=type("R", (), {
        "status_code": 200,
        "raise_for_status": lambda self: None,
        "json": lambda self: {"downloadUrl": "http://down.dingtalk.com/c2c/abc.jpg"},
    })())
    http.get = AsyncMock(return_value=FakeResponse())

    attachments = await download_inbound_attachments(
        http,
        message,
        inbound_dir=tmp_path / "inbound",
        access_token="tok",
        client_id="app-key",
    )
    assert len(attachments) == 1
    assert http.get.await_args.args[0].startswith("https://")


@pytest.mark.asyncio
async def test_on_message_file_only_placeholder(tmp_path):
    from dingtalk_stream import ChatbotMessage

    config = DingTalkConfig(
        client_id="cid",
        client_secret="sec",
        card_template_id="",
        dm_policy="pairing",
    )
    adapter = DingTalkAdapter(config, tmp_path)
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()

    fake_attachment = MagicMock()
    fake_attachment.kind = "file"

    message = ChatbotMessage.from_dict({
        "msgtype": "file",
        "content": {"downloadCode": "file-code", "fileName": "resume.pdf"},
        "conversationId": "chat-file",
        "msgId": "file1",
        "robotCode": "cid",
        "conversationType": "1",
        "senderId": "u1",
        "senderStaffId": "staff1",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=x",
        "sessionWebhookExpiredTime": 0,
    })

    with patch.object(adapter, "_get_access_token", new=AsyncMock(return_value="token")), patch(
        "sophagent.im.platforms.dingtalk.adapter.download_inbound_attachments",
        new=AsyncMock(return_value=[fake_attachment]),
    ), patch.object(adapter, "_spawn_thinking_reaction") as thinking:
        await adapter._on_message(message, driver, allowed=set())

    thinking.assert_called_once()
    driver.handle_inbound.assert_awaited_once()
    ev = driver.handle_inbound.await_args.args[0]
    assert ev.text == "[文件]"
    assert ev.attachments == [fake_attachment]


@pytest.mark.asyncio
async def test_on_message_image_only_placeholder(tmp_path):
    config = DingTalkConfig(
        client_id="cid",
        client_secret="sec",
        card_template_id="",
        dm_policy="pairing",
    )
    adapter = DingTalkAdapter(config, tmp_path)
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()

    fake_attachment = MagicMock()
    fake_attachment.kind = "image"

    message = SimpleNamespace(
        message_id="img1",
        conversation_id="chat-img",
        conversation_type="1",
        sender_id="u1",
        sender_staff_id="staff1",
        session_webhook="https://oapi.dingtalk.com/robot/send?access_token=x",
        session_webhook_expired_time=0,
        is_in_at_list=False,
        text=SimpleNamespace(content=""),
        image_content=SimpleNamespace(download_code="https://down.dingtalk.com/x.jpg"),
    )

    with patch.object(adapter, "_get_access_token", new=AsyncMock(return_value="token")), patch(
        "sophagent.im.platforms.dingtalk.adapter.download_inbound_attachments",
        new=AsyncMock(return_value=[fake_attachment]),
    ), patch.object(adapter, "_spawn_thinking_reaction"):
        await adapter._on_message(message, driver, allowed=set())

    driver.handle_inbound.assert_awaited_once()
    ev = driver.handle_inbound.await_args.args[0]
    assert ev.text == "[图片]"
    assert ev.attachments == [fake_attachment]

"""DingTalk robot emoji reactions (Thinking / Done)."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _robot_sdk_available() -> bool:
    try:
        import alibabacloud_dingtalk.robot_1_0 as _  # noqa: F401
        return True
    except ImportError:
        return False


class DingTalkEmotionClient:
    def __init__(self, *, robot_code: str) -> None:
        self.robot_code = robot_code
        self._sdk: Any = None
        if _robot_sdk_available():
            from alibabacloud_dingtalk.robot_1_0 import client as dingtalk_robot_client
            from alibabacloud_tea_openapi import models as open_api_models

            sdk_config = open_api_models.Config()
            sdk_config.protocol = "https"
            sdk_config.region_id = "central"
            self._sdk = dingtalk_robot_client.Client(sdk_config)

    @property
    def available(self) -> bool:
        return self._sdk is not None and bool(self.robot_code)

    async def send_emotion(
        self,
        *,
        access_token: str,
        open_msg_id: str,
        open_conversation_id: str,
        emoji_name: str,
        recall: bool = False,
    ) -> None:
        if not self.available or not access_token or not open_msg_id or not open_conversation_id:
            return
        from alibabacloud_dingtalk.robot_1_0 import models as dingtalk_robot_models
        from alibabacloud_tea_util import models as tea_util_models

        runtime = tea_util_models.RuntimeOptions()
        emotion_kwargs = {
            "robot_code": self.robot_code,
            "open_msg_id": open_msg_id,
            "open_conversation_id": open_conversation_id,
            "emotion_type": 2,
            "emotion_name": emoji_name,
        }
        try:
            if recall:
                emotion_kwargs["text_emotion"] = (
                    dingtalk_robot_models.RobotRecallEmotionRequestTextEmotion(
                        emotion_id="2659900",
                        emotion_name=emoji_name,
                        text=emoji_name,
                        background_id="im_bg_1",
                    )
                )
                request = dingtalk_robot_models.RobotRecallEmotionRequest(**emotion_kwargs)
                headers = dingtalk_robot_models.RobotRecallEmotionHeaders(
                    x_acs_dingtalk_access_token=access_token,
                )
                await self._sdk.robot_recall_emotion_with_options_async(request, headers, runtime)
            else:
                emotion_kwargs["text_emotion"] = (
                    dingtalk_robot_models.RobotReplyEmotionRequestTextEmotion(
                        emotion_id="2659900",
                        emotion_name=emoji_name,
                        text=emoji_name,
                        background_id="im_bg_1",
                    )
                )
                request = dingtalk_robot_models.RobotReplyEmotionRequest(**emotion_kwargs)
                headers = dingtalk_robot_models.RobotReplyEmotionHeaders(
                    x_acs_dingtalk_access_token=access_token,
                )
                await self._sdk.robot_reply_emotion_with_options_async(request, headers, runtime)
        except Exception as exc:
            log.debug("dingtalk: emotion %s failed: %s", emoji_name, exc)

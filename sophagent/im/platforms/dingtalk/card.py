"""DingTalk AI Card streaming via alibabacloud-dingtalk SDK."""

from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

MAX_CARD_CONTENT = 20000


def _card_sdk_available() -> bool:
    try:
        import alibabacloud_dingtalk.card_1_0 as _  # noqa: F401
        return True
    except ImportError:
        return False


class DingTalkCardClient:
    def __init__(self, *, card_template_id: str, robot_code: str) -> None:
        self.card_template_id = card_template_id
        self.robot_code = robot_code
        self._sdk: Any = None
        if _card_sdk_available() and card_template_id:
            from alibabacloud_dingtalk.card_1_0 import client as dingtalk_card_client
            from alibabacloud_tea_openapi import models as open_api_models

            sdk_config = open_api_models.Config()
            sdk_config.protocol = "https"
            sdk_config.region_id = "central"
            self._sdk = dingtalk_card_client.Client(sdk_config)

    @property
    def available(self) -> bool:
        return self._sdk is not None and bool(self.card_template_id)

    async def create_and_stream(
        self,
        *,
        access_token: str,
        message: Any,
        content: str,
        finalize: bool,
    ) -> str | None:
        if not self.available:
            return None
        from alibabacloud_dingtalk.card_1_0 import models as dingtalk_card_models
        from alibabacloud_tea_util import models as tea_util_models

        out_track_id = f"soph_{uuid.uuid4().hex[:12]}"
        runtime = tea_util_models.RuntimeOptions()
        create_request = dingtalk_card_models.CreateCardRequest(
            card_template_id=self.card_template_id,
            out_track_id=out_track_id,
            card_data=dingtalk_card_models.CreateCardRequestCardData(
                card_param_map={"content": ""},
            ),
            callback_type="STREAM",
            im_group_open_space_model=dingtalk_card_models.CreateCardRequestImGroupOpenSpaceModel(
                support_forward=True,
            ),
            im_robot_open_space_model=dingtalk_card_models.CreateCardRequestImRobotOpenSpaceModel(
                support_forward=True,
            ),
        )
        create_headers = dingtalk_card_models.CreateCardHeaders(
            x_acs_dingtalk_access_token=access_token,
        )
        await self._sdk.create_card_with_options_async(create_request, create_headers, runtime)

        conversation_id = getattr(message, "conversation_id", "") or ""
        conversation_type = getattr(message, "conversation_type", "1")
        is_group = str(conversation_type) == "2"
        sender_staff_id = getattr(message, "sender_staff_id", "") or ""

        if is_group:
            open_space_id = f"dtv1.card//IM_GROUP.{conversation_id}"
            deliver_request = dingtalk_card_models.DeliverCardRequest(
                out_track_id=out_track_id,
                user_id_type=1,
                open_space_id=open_space_id,
                im_group_open_deliver_model=dingtalk_card_models.DeliverCardRequestImGroupOpenDeliverModel(
                    robot_code=self.robot_code,
                ),
            )
        else:
            if not sender_staff_id:
                log.warning("dingtalk: AI card skipped for DM without sender_staff_id")
                return None
            open_space_id = f"dtv1.card//IM_ROBOT.{sender_staff_id}"
            deliver_request = dingtalk_card_models.DeliverCardRequest(
                out_track_id=out_track_id,
                user_id_type=1,
                open_space_id=open_space_id,
                im_robot_open_deliver_model=dingtalk_card_models.DeliverCardRequestImRobotOpenDeliverModel(
                    space_type="IM_ROBOT",
                ),
            )
        deliver_headers = dingtalk_card_models.DeliverCardHeaders(
            x_acs_dingtalk_access_token=access_token,
        )
        await self._sdk.deliver_card_with_options_async(deliver_request, deliver_headers, runtime)
        await self.stream_update(
            access_token=access_token,
            out_track_id=out_track_id,
            content=content,
            finalize=finalize,
        )
        return out_track_id

    async def stream_update(
        self,
        *,
        access_token: str,
        out_track_id: str,
        content: str,
        finalize: bool,
    ) -> None:
        if not self.available:
            raise RuntimeError("DingTalk card SDK unavailable")
        from alibabacloud_dingtalk.card_1_0 import models as dingtalk_card_models
        from alibabacloud_tea_util import models as tea_util_models

        stream_request = dingtalk_card_models.StreamingUpdateRequest(
            out_track_id=out_track_id,
            guid=str(uuid.uuid4()),
            key="content",
            content=content[:MAX_CARD_CONTENT],
            is_full=True,
            is_finalize=finalize,
            is_error=False,
        )
        stream_headers = dingtalk_card_models.StreamingUpdateHeaders(
            x_acs_dingtalk_access_token=access_token,
        )
        runtime = tea_util_models.RuntimeOptions()
        await self._sdk.streaming_update_with_options_async(stream_request, stream_headers, runtime)

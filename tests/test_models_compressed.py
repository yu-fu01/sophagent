"""Message.compressed 标志位的序列化往返测试。"""

from sophagent.models import Message


def test_compressed_defaults_false_and_omitted():
    m = Message(role="user", content="hi")
    assert m.compressed is False
    assert "_compressed" not in m.to_dict()


def test_compressed_true_serialized_with_underscore_key():
    m = Message(role="user", content="summary", compressed=True)
    d = m.to_dict()
    assert d["_compressed"] is True


def test_compressed_roundtrip_through_json():
    m = Message(role="user", content="summary", compressed=True)
    back = Message.from_json(m.to_json())
    assert back.compressed is True
    assert back.content == "summary"


def test_from_dict_without_flag_is_false():
    back = Message.from_dict({"role": "user", "content": "x"})
    assert back.compressed is False

"""文件管理 API 端到端测试（/api/files），复用 conftest 的 client/bob 夹具。"""

import base64


def data_url(content: bytes, mime: str = "text/plain") -> str:
    return f"data:{mime};base64," + base64.b64encode(content).decode()


def test_upload_list_read_roundtrip(client, bob):
    # 上传一个文本文件
    resp = client.post("/api/files/upload",
                       json={"path": "notes.txt", "data_url": data_url(b"hello world")},
                       headers=bob)
    assert resp.status_code == 200, resp.text
    assert resp.json()["entry"]["name"] == "notes.txt"

    # 列根目录能看到它
    listing = client.get("/api/files", headers=bob).json()
    names = [e["name"] for e in listing["entries"]]
    assert "notes.txt" in names
    assert listing["parent"] is None

    # 读回内容
    read = client.get("/api/files/read", params={"path": "notes.txt"}, headers=bob).json()
    assert read["content"] == "hello world"
    assert read["size"] == 11


def test_path_escape_rejected(client, bob):
    # 列目录时 .. 逃逸被拒
    assert client.get("/api/files", params={"path": "../"}, headers=bob).status_code == 400
    # 读绝对路径被拒
    assert client.get("/api/files/read", params={"path": "/etc/passwd"}, headers=bob).status_code == 400


def test_upload_too_large(client, admin, bob):
    # admin 把上限调到 1KB（运行时设置，落 DB），上传 2KB 应被拒
    client.put("/api/settings/max_upload_bytes",
               json={"max_upload_bytes": 1024}, headers=admin)
    resp = client.post("/api/files/upload",
                       json={"path": "big.txt", "data_url": data_url(b"x" * 2000)},
                       headers=bob)
    assert resp.status_code == 413


def test_upload_dedupe(client, bob):
    body = {"path": "dup.txt", "data_url": data_url(b"a")}
    first = client.post("/api/files/upload", json=body, headers=bob).json()
    second = client.post("/api/files/upload", json=body, headers=bob).json()
    assert first["entry"]["name"] == "dup.txt"
    assert second["entry"]["name"] == "dup (1).txt"


def test_read_binary_returns_data_url(client, bob):
    payload = bytes([0, 1, 2, 255, 254])
    client.post("/api/files/upload",
                json={"path": "blob.bin", "data_url": data_url(payload, "application/octet-stream")},
                headers=bob)
    read = client.get("/api/files/read", params={"path": "blob.bin"}, headers=bob).json()
    assert "content" not in read
    assert read["data_url"].startswith("data:application/octet-stream;base64,")


def test_unauthenticated(client):
    assert client.get("/api/files").status_code == 401
    assert client.post("/api/files/upload", json={"path": "x", "data_url": data_url(b"x")}).status_code == 401

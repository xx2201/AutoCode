"""
tests/conftest.py
=================
Pytest 配置：用内存中的 FakeRedis 替换掉真 Redis，
并把 app 启动所需的目录 (templates / static) 指到项目根。

每个测试函数前自动注入一颗干净 Redis。
"""

import os
import sys
import time
import threading

import pytest

# 把项目根加进 sys.path，保证 `import app` / `import session_store` 没问题
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


# ---------- 一个足够用的 FakeRedis ----------
# 只实现 session_store 实际用到的命令：
#   hset, hgetall, expire, ttl, delete, scan, ping
class FakeRedis:
    def __init__(self):
        self._lock = threading.RLock()
        # store: key -> {"hash": {field: value}, "expire_at": float|None}
        self.store = {}

    # ---- 内部 ----
    def _now(self):
        return time.time()

    def _is_alive(self, key):
        item = self.store.get(key)
        if item is None:
            return False
        exp = item.get("expire_at")
        if exp is not None and exp <= self._now():
            self.store.pop(key, None)
            return False
        return True

    def _purge_if_expired(self, key):
        # 大多数命令调用前都应先判断
        item = self.store.get(key)
        if item is None:
            return None
        exp = item.get("expire_at")
        if exp is not None and exp <= self._now():
            self.store.pop(key, None)
            return None
        return item

    # ---- hash ----
    def hset(self, key, mapping=None, **kwargs):
        with self._lock:
            item = self._purge_if_expired(key) or {"hash": {}, "expire_at": None}
            for k, v in (mapping or {}).items():
                item["hash"][k] = v
            self.store[key] = item
            return len(mapping or {})

    def hgetall(self, key):
        with self._lock:
            item = self._purge_if_expired(key)
            if not item:
                return {}
            return dict(item["hash"])

    # ---- key ----
    def expire(self, key, seconds):
        with self._lock:
            if key not in self.store:
                return 0
            self.store[key]["expire_at"] = self._now() + int(seconds)
            return 1

    def ttl(self, key):
        with self._lock:
            if key not in self.store:
                return -2  # key 不存在
            exp = self.store[key].get("expire_at")
            if exp is None:
                return -1  # 不过期
            remain = int(exp - self._now())
            if remain < 0:
                self.store.pop(key, None)
                return -2
            return remain

    def delete(self, *keys):
        with self._lock:
            n = 0
            for k in keys:
                if k in self.store:
                    self.store.pop(k, None)
                    n += 1
            return n

    # ---- scan (教学演示 list_all_sessions 用) ----
    def scan(self, cursor=0, match=None, count=100):
        with self._lock:
            # 简化：一次性返回所有匹配 key，cursor 永远回到 0
            keys = [k for k in list(self.store.keys()) if not match or _fnmatch(k, match)]
            return 0, keys

    def ping(self):
        return True


def _fnmatch(name, pattern):
    """极简通配：只支持结尾的 '*'，session 用法足够。"""
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return name == pattern


# ---------- fixtures ----------
@pytest.fixture
def fake_redis(monkeypatch):
    """用 FakeRedis 替换 session_store.r，并 import app。"""
    import session_store
    fake = FakeRedis()
    monkeypatch.setattr(session_store, "r", fake)
    # 重新 import app，确保它用到的 session_store.r 也是 fake
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    return app_module, fake


@pytest.fixture
def client(fake_redis):
    app_module, _ = fake_redis
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)


# ---------- 助手 ----------
def login_as(client, username="alice", password="123"):
    """用表单登录，返回登录后拿到的 cookie jar。"""
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"login failed: {resp.status_code} {resp.text}"
    return resp

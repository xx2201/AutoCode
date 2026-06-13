"""
tests/test_app.py
=================
覆盖：
- 登录 / 登出基本流程
- /home 刷新 Redis TTL + cookie max_age
- /admin 只允许 alice (user_id == 1)
- 未登录访问受保护页面 -> 跳 /login
- 普通用户访问 /admin -> 403
"""

import time


# ---------- 基础流程 ----------

def test_root_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "登录" in r.text


def test_login_with_wrong_password(client):
    r = client.post(
        "/login",
        data={"username": "alice", "password": "wrong"},
        follow_redirects=False,
    )
    # 业务上 400，模板里显示错误
    assert r.status_code == 400
    assert "用户名或密码错误" in r.text


def test_login_and_home_renders(client):
    r = client.post(
        "/login",
        data={"username": "alice", "password": "123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/home"
    # 登录成功会 set SID cookie
    assert "SID" in r.cookies

    r2 = client.get("/home")
    assert r2.status_code == 200
    assert "alice" in r2.text
    # TTL 应在页面里被渲染出来
    assert "TTL" in r2.text or "秒" in r2.text


def test_logout_clears_cookie_and_session(client):
    client.post(
        "/login",
        data={"username": "alice", "password": "123"},
        follow_redirects=False,
    )
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # 之后 /home 应当跳回 /login（session 没了）
    r2 = client.get("/home", follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/login"


# ---------- 权限：未登录 ----------

def test_home_without_session_redirects_to_login(client):
    r = client.get("/home", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_admin_without_session_redirects_to_login(client):
    r = client.get("/admin", follow_redirects=False)
    # 同样先被 current_session 拦截
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---------- 权限：/admin 只允许 alice ----------

def test_admin_accessible_for_alice(client):
    client.post(
        "/login",
        data={"username": "alice", "password": "123"},
        follow_redirects=False,
    )
    r = client.get("/admin")
    assert r.status_code == 200
    assert "管理员" in r.text
    assert "alice" in r.text


def test_admin_forbidden_for_bob(client):
    client.post(
        "/login",
        data={"username": "bob", "password": "123"},
        follow_redirects=False,
    )
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 403
    assert "403" in r.text
    assert "权限" in r.text or "管理员" in r.text


def test_admin_forbidden_for_tom(client):
    client.post(
        "/login",
        data={"username": "tom", "password": "123"},
        follow_redirects=False,
    )
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 403


def test_home_shows_admin_link_for_alice_only(client):
    # alice 应该看到进入管理员页的链接
    client.post(
        "/login",
        data={"username": "alice", "password": "123"},
        follow_redirects=False,
    )
    r = client.get("/home")
    assert r.status_code == 200
    assert "/admin" in r.text
    assert "进入管理员页面" in r.text

    # 登出
    client.get("/logout", follow_redirects=False)

    # bob 不应该看到
    client.post(
        "/login",
        data={"username": "bob", "password": "123"},
        follow_redirects=False,
    )
    r2 = client.get("/home")
    assert r2.status_code == 200
    assert "进入管理员页面" not in r2.text


# ---------- TTL 刷新 + cookie 刷新 ----------

def test_home_refreshes_redis_ttl(client, fake_redis):
    """访问 /home 后，Redis 里该 session 的 TTL 应当被重置回 SESSION_TTL。"""
    app_module, fake = fake_redis
    import session_store

    client.post(
        "/login",
        data={"username": "alice", "password": "123"},
        follow_redirects=False,
    )
    # 找到刚创建的 session key
    keys = [k for k in fake.store.keys() if k.startswith(session_store.SESSION_PREFIX)]
    assert len(keys) == 1
    sid = keys[0].removeprefix(session_store.SESSION_PREFIX)

    # 手动把 TTL 调小 (例如 5 秒)，模拟"接近过期"
    assert session_store.touch_session(sid) is True
    fake.store[sid]["expire_at"] = time.time() + 5

    ttl_before = session_store.get_ttl(sid)
    assert 0 < ttl_before <= 5

    # 访问 /home 应当把 TTL 刷回 SESSION_TTL
    r = client.get("/home")
    assert r.status_code == 200

    ttl_after = session_store.get_ttl(sid)
    assert ttl_after > ttl_before
    # 应该非常接近 SESSION_TTL
    assert ttl_after >= session_store.SESSION_TTL - 2


def test_home_refreshes_cookie_max_age(client, fake_redis):
    """访问 /home 应当在响应里 set-cookie 一个新的 SID，且 max-age == SESSION_TTL。"""
    app_module, fake = fake_redis
    import session_store

    client.post(
        "/login",
        data={"username": "alice", "password": "123"},
        follow_redirects=False,
    )

    r = client.get("/home")
    assert r.status_code == 200
    # 响应里应当有 Set-Cookie: SID=...; ... Max-Age=SESSION_TTL
    set_cookie = r.headers.get("set-cookie", "")
    assert "SID=" in set_cookie
    assert f"Max-Age={session_store.SESSION_TTL}" in set_cookie
    # httponly 也应当带上
    assert "HttpOnly" in set_cookie

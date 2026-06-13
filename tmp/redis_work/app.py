"""
app.py
======
FastAPI 主程序：登录/首页/登出 + Redis session 检视页

教学重点：
    - 自己实现 session 存到 Redis（不依赖框架自带的 cookie session）
    - 浏览器只存一个 session_id（cookie），真实数据都在 Redis
    - 配合 /inspect 页面，可以在浏览器里直接看到 Redis 里 session 的样子

相比 Flask 的差异：
    - 用 Depends + 自定义异常实现「登录保护」
    - 用 Jinja2Templates 渲染 HTML
    - 用 RedirectResponse 处理跳转
"""

import session_store

from fastapi import (
    FastAPI,
    Request,
    Form,
    Response,
    Depends,
    status,
)
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


app = FastAPI(title="Redis Session 教学演示")

# 静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")

# 模板
templates = Jinja2Templates(directory="templates")


# ---------- 教学用"假用户库" ----------
USERS = {
    "alice": {"password": "123", "user_id": 1, "nickname": "爱丽丝"},
    "bob":   {"password": "123", "user_id": 2, "nickname": "鲍勃"},
    "tom":   {"password": "123", "user_id": 3, "nickname": "汤姆"},
}


# cookie 里 session_id 的名字
COOKIE_NAME = "SID"


# ---------- url_for 助手：让模板里的 url_for() 继续工作 ----------
#
# Flask 里模板写 {{ url_for('home') }} 自动生成 /home
# FastAPI 没有这个内置，但我们可以注册一个 Jinja2 global，
# 模板一行都不用改。

ROUTES = {
    "index":      "/",
    "login":      "/login",
    "home":       "/home",
    "logout":     "/logout",
    "inspect":    "/inspect",
    "expire_now": "/expire_now",
    "admin":      "/admin",
}


def url_for(name, **params):
    """极简版 url_for：支持路由名 + static"""
    if name == "static":
        # 模板里写的是 url_for('static', filename='style.css')
        return f"/static/{params.get('filename', params.get('path', ''))}"
    if name not in ROUTES:
        raise ValueError(f"未知路由: {name}")
    return ROUTES[name]


templates.env.globals["url_for"] = url_for


# ---------- 自定义异常：登录保护时跳转到 /login ----------

class LoginRequired(Exception):
    """被登录保护装饰的路由，发现未登录时抛出"""
    def __init__(self, location="/login"):
        self.location = location


class Forbidden(Exception):
    """已登录但权限不足（例如访问 /admin 的非 admin 用户）"""
    def __init__(self, message="权限不足"):
        self.message = message


@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, exc: LoginRequired):
    return RedirectResponse(url=exc.location, status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(Forbidden)
async def forbidden_handler(request: Request, exc: Forbidden):
    return templates.TemplateResponse(
        "403.html",
        {"request": request, "message": exc.message},
        status_code=status.HTTP_403_FORBIDDEN,
    )


def current_session(request: Request) -> dict:
    """
    依赖项：从 cookie 拿 SID，去 Redis 取 session。
    取不到就抛出 LoginRequired，由全局处理器跳转。
    """
    sid = request.cookies.get(COOKIE_NAME)
    sess = session_store.get_session(sid)
    if not sess:
        raise LoginRequired()
    return {"sid": sid, "data": sess}


# ---------- 路由 ----------


@app.get("/")
def index():
    """根路径：有 session 跳首页，否则跳登录页"""
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login")
def login_get(request: Request):
    """显示登录表单（GET）"""
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    """处理登录表单（POST）"""
    username = username.strip()
    user = USERS.get(username)

    if not user or user["password"] != password:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "用户名或密码错误"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # 关键步骤：创建 session 并写入 Redis
    sid = session_store.create_session(
        user_id=user["user_id"],
        username=username,
    )

    # 把 session_id 塞到 cookie 里返回给浏览器
    response = RedirectResponse(url="/home", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE_NAME, sid,
        max_age=session_store.SESSION_TTL,
        httponly=True,  # JS 读不到，更安全
    )
    return response


@app.get("/home")
def home(request: Request, response: Response, ctx: dict = Depends(current_session)):
    """首页：展示当前 session 信息 + 刷新 Redis TTL + 刷新 cookie max_age"""
    # 1) 刷新 Redis 里的 TTL（滑动过期：用户持续活跃就不掉线）
    session_store.touch_session(ctx["sid"])
    # 2) 同步刷新浏览器 cookie 的 max_age，避免 cookie 比 server session 早死
    response.set_cookie(
        COOKIE_NAME, ctx["sid"],
        max_age=session_store.SESSION_TTL,
        httponly=True,
    )
    ttl = session_store.get_ttl(ctx["sid"])
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": ctx["data"],
            "sid": ctx["sid"],
            "ttl": ttl,
            "is_admin": str(ctx["data"].get("user_id")) == "1",
        },
    )


@app.get("/logout")
def logout(request: Request):
    """登出：删 Redis 里的 key + 删浏览器 cookie"""
    sid = request.cookies.get(COOKIE_NAME)
    session_store.destroy_session(sid)
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/inspect")
def inspect(request: Request):
    """教学重点：Redis 检视页（无需登录，方便大屏演示）"""
    sessions = session_store.list_all_sessions()
    redis_ok = session_store.ping()
    return templates.TemplateResponse(
        "redis_inspect.html",
        {
            "request": request,
            "sessions": sessions,
            "redis_ok": redis_ok,
            "ttl_setting": session_store.SESSION_TTL,
        },
    )


@app.get("/expire_now")
def expire_now(ctx: dict = Depends(current_session)):
    """可选：手动让当前 session 失效（演示过期）"""
    session_store.r.expire(session_store.SESSION_PREFIX + ctx["sid"], 5)
    return RedirectResponse(url="/home", status_code=status.HTTP_303_SEE_OTHER)


# ---------- 权限演示：/admin 只允许 alice (user_id == 1) ----------

# 简单的"管理员判定"，教学用：user_id == 1 就是管理员
def is_admin(user: dict) -> bool:
    return str(user.get("user_id")) == "1"


@app.get("/admin")
def admin(request: Request, ctx: dict = Depends(current_session)):
    """管理员页面：只有 alice (user_id == 1) 能进；其他登录用户 403"""
    if not is_admin(ctx["data"]):
        # 已登录但不是管理员 -> 403 友好提示页
        raise Forbidden(message="该页面仅管理员 (alice) 可访问")
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": ctx["data"],
            "sid": ctx["sid"],
        },
    )


# ---------- 启动 ----------

if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("Redis Session 教学演示 (FastAPI 版)")
    print("  访问 http://127.0.0.1:5000/login  开始")
    print("  访问 http://127.0.0.1:5000/inspect 查看 Redis 里的 session")
    print("  测试账号: alice / 123,  bob / 123,  tom / 123")
    print("=" * 60)
    uvicorn.run("app:app", host="127.0.0.1", port=5000, reload=True)

"""

session_store.py

================

教学重点：把 Session 存到 Redis 的核心封装



为什么用 Redis 存 session？

- 内存读写，毫秒级响应

- 天然支持过期时间（TTL），不用自己写清理逻辑

- 多实例部署时，session 可以跨服务器共享（数据库 session 做不到）



Redis Key 设计：

    session:<随机id>     -> Hash，包含 {user_id, username, login_time, ...}

                            并设置 EXPIRE，让 session 自动过期



为什么用 Hash 而不是 String？

- Hash 可以存多个字段，结构清晰，便于演示

- HMGET 可以只取部分字段，省带宽

- 教学效果更好：能直接看到 Redis 里到底存了什么

"""

import secrets

import time

from typing import Optional



import redis



# ---------- 连接配置 ----------

# decode_responses=True 让返回的是 str 而不是 bytes，便于演示和调试

r = redis.Redis(

    host="localhost",

    port=6379,

    db=0,

    decode_responses=True,

)



# session 在 Redis 中的 key 前缀

SESSION_PREFIX = "session:"



# session 有效期（秒），教学演示设短一点，方便看过期

SESSION_TTL = 60 * 30  # 30 分钟





# ---------- 核心 API ----------



def create_session(user_id: int, username: str) -> str:

    """

    创建一个新 session，写入 Redis 并返回 session_id。



    实际发生了什么：

        1. 生成一个 256 bit 随机字符串作为 session_id

        2. 在 Redis 里建一个 Hash key = "session:<session_id>"

        3. 把用户信息作为 field/value 写进去

        4. 给这个 key 设置 EXPIRE 30 分钟

    """

    # 1. 生成 session_id（URL 安全的随机串）

    session_id = secrets.token_urlsafe(32)



    # 2. 拼成完整的 Redis key

    key = SESSION_PREFIX + session_id



    # 3. 用 HSET 把 session 内容写进 Hash

    #    等价于:  HSET session:xxx user_id 1 username alice login_time 1700000000

    r.hset(key, mapping={

        "user_id": user_id,

        "username": username,

        "login_time": int(time.time()),

    })



    # 4. 设置过期时间（关键！这就是 Redis 替代"定时清理过期 session"的核心）

    r.expire(key, SESSION_TTL)



    return session_id





def get_session(session_id: str) -> Optional[dict]:

    """

    根据 session_id 从 Redis 读出 session 内容。

    如果 key 不存在（过期了或被删了）返回 None。

    """

    if not session_id:

        return None

    key = SESSION_PREFIX + session_id

    data = r.hgetall(key)

    return data if data else None





def destroy_session(session_id: str) -> bool:

    """登出时删除 session 对应的 Redis key。"""

    if not session_id:

        return False

    key = SESSION_PREFIX + session_id

    # DEL 返回被删除的 key 数量，1 表示删成功

    return r.delete(key) == 1





def get_ttl(session_id: str) -> int:

    """查看 session 还有多少秒过期（教学演示用）。"""

    return r.ttl(SESSION_PREFIX + session_id)




def touch_session(session_id: str) -> bool:

    """

    刷新 session 的 TTL 到 SESSION_TTL（滑动过期）。



    用于：用户每次访问受保护页面时，把 session 的"生命倒计时"重置回 30 分钟。

    只要用户持续活跃，session 就不会过期；长时间不操作才会真正过期。

    """

    if not session_id:

        return False

    key = SESSION_PREFIX + session_id

    # 只在 key 存在时刷新 TTL；避免给已过期的 key"续命"

    return r.expire(key, SESSION_TTL) == 1





# ---------- 教学演示专用 ----------



def list_all_sessions() -> list[dict]:

    """

    列出 Redis 里所有 session，用于教学可视化页面。

    生产环境千万别用 SCAN + KEYS 模式（KEYS 会阻塞 Redis），

    这里用 SCAN 是因为 session 数量很小，演示安全。

    """

    result = []

    cursor = 0

    while True:

        cursor, keys = r.scan(cursor=cursor, match=SESSION_PREFIX + "*", count=100)

        for key in keys:

            data = r.hgetall(key)

            data["__key__"] = key

            data["__ttl__"] = r.ttl(key)

            result.append(data)

        if cursor == 0:

            break

    return result





def ping() -> bool:

    """测试 Redis 连通性。"""

    try:

        return r.ping()

    except Exception:

        return False

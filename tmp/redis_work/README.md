# Redis Session 教学演示



> 一个用 FastAPI + Redis 演示 **"Session 是怎么缓存在 Redis 里"** 的极简项目。

> 前端只用了原生 HTML/CSS，专注后端原理。



---



## 一、它解决什么问题？



HTTP 是无状态的：服务器不知道"我是谁"。传统做法是在服务器内存里维护 session 表，

缺点是**重启 / 多机部署就丢**。把 session 放进 Redis 后：



- **快**: 内存读写,毫秒级

- **共享**: 多台 FastAPI 实例共用一份 Redis

- **自动过期**: Redis 的 TTL 机制帮你定时清理,不用写定时任务



---



## 二、怎么跑



### 1. 准备 Redis



```

# Windows 上如未装 redis,可去下面地址下载 Windows 版

# https://github.com/tporadowski/redis/releases

# 启动后默认监听 6379

redis-cli ping     # 应返回 PONG

```



### 2. 装依赖 & 启动



```

pip install -r requirements.txt

python app.py    # 等价于: uvicorn app:app --reload

# 跑测试 (无需真 Redis,conftest.py 用内存 FakeRedis 替换)
pytest -q

```



浏览器打开 `http://127.0.0.1:5000`,登录账号 `alice / 123`。



---



## 三、关键文件



| 文件 | 作用 |

|---|---|

| `session_store.py` | **教学重点**: 把 session 写入 / 读取 / 删除 Redis 的核心封装 |

| `app.py` | FastAPI 路由: 登录 / 首页 / 登出 / Redis 检视页 / 管理员页 |

| `templates/` | 6 个 HTML 模板 (base / home / login / redis_inspect / admin / 403) |

| `static/style.css` | 简单样式 |

| `tests/` | pytest 测试,使用内存 FakeRedis 替代真 Redis |



---



## 四、推荐教学演示流程



> 准备 3 个浏览器标签页: A 登录页、B 首页、C Redis 检视页



1. **打开 C 标签页 (Redis 检视页)**

   - 看到 "Redis 中还没有 session"



2. **A 标签页用 `alice` 登录**

   - 跳转到首页,显示欢迎信息

   - **同时 C 标签页 (或刷新它)**

   - 看到一个新 `session:xxxxx` 的 Hash,里面有 `user_id=1, username=alice, login_time=...`

   - 后面跟着 **TTL: 1800 秒**



3. **首页底部点 "把 TTL 改成 5 秒"**

   - C 标签页刷新 -> 看到 TTL 变成 5

   - **等 5 秒后再刷新** -> session 块消失,回到空状态

   - 这就是 Redis 的"自动过期"



4. **A 重新登录 -> 点首页右上"登出"**

   - 浏览器 cookie 被删,**同时 Redis 里那条 key 也被 DEL**

   - C 标签页刷新 -> 又空了



5. **进阶演示: 开两个隐身窗口分别用 alice 和 bob 登录**

   - C 标签页同时显示两条 session

   - 各自独立,互不影响 -> 这就是"会话隔离"



6. **权限演示: `/admin` 只允许 alice**

   - 用 `alice / 123` 登录后访问首页 -> 看到「🛡️ 进入管理员页面」按钮

   - 用 `bob / 123` 或 `tom / 123` 登录后访问 `/admin` -> 看到 403 友好提示页

   - 未登录访问 `/admin` -> 仍然 303 跳 `/login` (登录保护先于权限检查)

   - 这就是"基于 session 的权限控制": 同一份 cookie,不同用户看到不同结果


---



## 五、核心知识点对照



### 浏览器 <-> Redis 数据流



```

+----------------+        +--------------------+

|   浏览器 Cookie|        |     Redis          |

|   SID=xxxxx    | -----> | session:xxxxx      |

+----------------+   ^    |  HSET user_id 1    |

              GET / |     |  HSET username ..  |

                     |     |  EXPIRE 1800       |

                     |     +--------------------+

       业务请求时 cookie 自动带上 SID

       FastAPI 拿 SID 去 Redis HGETALL 拿到用户信息

```



### 用到的 Redis 命令



| 命令 | 用途 | 在代码中的位置 |

|---|---|---|

| `HSET key f1 v1 f2 v2` | 把 session 内容写进 Hash | `create_session` |

| `EXPIRE key 1800` | 设 30 分钟过期 | `create_session` |

| `HGETALL key` | 读出整个 Hash | `get_session` |

| `TTL key` | 看剩余过期时间 | `get_ttl` |

| `DEL key` | 登出时删除 | `destroy_session` |

| `SCAN MATCH session:*` | 列出所有 session | `list_all_sessions` |

| `EXPIRE key SESSION_TTL` | 用户活跃时滑动续期 | `touch_session` (在 /home 调用) |



### 为什么用 Hash 而不是 String?



- **String** 存整个 JSON: 取一个字段要反序列化整个对象

- **Hash** 存字段: 可以 `HMGET key user_id` 只取需要的字段,省带宽

- 教学上更直观: 能在 Redis 检视页直接看到 "这个 key 里有这些字段"



---



## 六、生产环境还需要做什么?



本项目只做教学,省略了很多生产细节。生产里请注意:



- [ ] **HTTPS only** + `Secure` cookie

- [ ] **session 加密 / 签名**,防伪造 (用 `itsdangerous`)

- [x] **session 续期 (滑动过期)**: 用户每次访问 `/home`,`session_store.touch_session(sid)` 会把该 session 的 TTL 重置回 `SESSION_TTL` (30 分钟);同时 `Set-Cookie` 的 `Max-Age` 也被刷新,避免 cookie 比 server session 早死

- [ ] **CSRF 防护**

- [ ] **Redis 密码** + 不用默认 db 0

- [ ] **不要 SCAN/KEYS 线上生产环境的 Redis** (本项目仅作教学用)



---



## 七、想要改造?



- 改 `SESSION_TTL`(在 `session_store.py` 顶部)观察过期

- 把 Hash 改成 String + JSON,对比两种数据结构的差异

- (已实现) `/admin` 路由: `user_id == 1` 才能进,否则 403 -> 演示"基于 session 的权限控制"

- 加单元测试覆盖更多边界: cookie 过期、并发 touch、session 被删后再访问等

- 引入 `pytest-asyncio` 测一下异步路由


---


## 八、/admin 与 Session 滑动续期



### `/admin` 权限演示



- **未登录**访问 `/admin` -> `current_session` 依赖抛 `LoginRequired` -> 303 跳 `/login`



- **已登录非管理员** (bob / tom) 访问 `/admin` -> 抛 `Forbidden` -> 渲染 `403.html` (HTTP 403)



- **alice** 访问 `/admin` -> 渲染 `admin.html`



- 首页对 alice 显示「🛡️ 进入管理员页面」按钮,其他人不显示



### Session 滑动续期 (sliding expiration)



访问受保护页面 (`/home`) 时,做两件事:



1. `session_store.touch_session(sid)` -> `EXPIRE session:<sid> SESSION_TTL`,把 Redis 里这条 key 的 TTL 重置回 30 分钟



2. `response.set_cookie(SID, sid, max_age=SESSION_TTL, httponly=True)` -> 同步刷新浏览器 cookie 的 `Max-Age`



只要用户持续活跃, session 永远不过期; 长时间不操作 (默认 30 分钟) 才真正过期。


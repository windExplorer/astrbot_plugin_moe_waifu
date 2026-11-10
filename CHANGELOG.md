# 更新日志

> 倒序（最新在上）。版本唯一来源为 `metadata.yaml` 的 `version`，条目号与其严格一致。

## v0.2.1 (2026-11-11)

修复工坊页面视图重叠（版本 v0.2.0 -> v0.2.1）。

- **现象**：浏览器冒烟发现角色库表格与立绘库网格同时出现、内容各渲染两份；
  切换菜单后旧视图残留，四个视图叠加显示。
- **根因**：in-DOM 模板里用 `<template v-if>` 包裹四个视图分块，而它们都在
  naive `n-space` 的默认槽里——n-space 对 `<template>` 分片的槽内容会重复渲染
  且 `v-if` 条件失效（in-DOM 模板已知坑的新变体，本地截图与 DOM 计数实证：
  `.n-data-table` ×2、`.n-grid` ×2）。头部条形区没有 template 包裹，渲染正常。
- **修复**：四个视图分块与抽屉里的 img2img 条件块全部改为 `<div v-if>` 包裹；
  冒烟复验：三视图互斥、各渲染一份、立绘库卡片数正确（2 张）。
- v0.2.0 本地包从未发布（含此缺陷），按打包纪律作废保留在 `dist/`。

## v0.2.0 (2026-11-11)

角色库工坊 WebUI（版本 v0.1.0 -> v0.2.0）。

- **需求（用户）**：试画、角色库管理这些操作都应该在 WebUI 里做，不该做成聊天指令
  ——v0.1.0 的 `/老婆试画` 过渡指令撤销。
- **新增插件 Page「角色库工坊」**（`pages/waifu-console/` 单文件 Vue3+NaiveUI，
  box member_panel 同款形态；`webui_api.py` 后端 13 条路由，桥接信封 `{"status":"ok"}`）：
  - **角色库**：卷/分类/验证/关键词筛选，新增与编辑抽屉（含画法策略三选一与各自
    专属字段、应援色取色器）、verified 开关、跨卷 id 查重、两段式删除确认
    （sandbox 无 confirm/alert）；
  - **试画台**：选角色 → 后台 job 生成 → 前端轮询（1.5s 间隔，180s 超时）→ 预览图；
    「画对了，标记 verified」与「存入立绘库」一键闭环；
  - **立绘库**：按角色图集缩略图网格（含孤立图发现）、两段式删图、单角色「补一张」
    （成功即归档）；
  - **分群绑定**：`group_pools` 多行文本编辑 + 解析预览 + 即时写回插件配置
    （schema 白名单键，`save_config()`）。
- **试画的事件问题（关键实现）**：萌绘 handler 会 `isinstance(event, AstrMessageEvent)`
  校验并在无效时回落其 `_last_event`（可能是任意会话的最后一条消息）。工坊试画合成
  一个最小桩事件（`platform=webui`，`session_id=moe_waifu_workshop`），保证不借用聊天
  会话上下文；构造失败回落 None（退化为萌绘默认行为）。
- **试画 job 内存管理**：`plugin._jobs` 字典 + `asyncio.create_task`，上限 40 个、
  满时清最旧的非运行 job；job 状态接口不回传 `src_path`（服务端持有，归档接口专用）。
- **删除立绘**：相对路径白名单形状校验（`portraits/<id>/<file>`，禁 `..` 穿越）后
  删文件 + 删 `portraits` 表行；低于配额的角色恢复按需补生。
- **打包**：`$includeList` 增补 `webui_api.py` 与 `pages`（含 vendored
  `vue.global.prod.js` / `naive-ui.min.js`，取自 moe_box member_panel 同源资产）；
  顶层 .py 守卫会拦截漏列。

## v0.1.0 (2026-11-11)

首个发布版本：萌萌今日老婆（版本从 v0.1.0 起计数）。

- **玩法**：`/今日老婆`（别名 `/老婆`）一人一天一位，种子决定论
  （SHA-256(date|uid|salt|nonce)，重启不变）；`/换老婆` 每日限次重抽；
  `/老婆帮助` 帮助。
- **立绘库**：每角色配额（默认 5 张）。未满配额时群聊首抽触发生成并存档，
  当日已生成直接复用（同群同款共享同一张图）；满配额后按种子取模从库存取图，
  **零绘图秒出**，对萌绘零依赖。立绘只增不淘汰，WebUI 删图后自动恢复补生（M2）。
- **萌绘联动（硬依赖）**：走 `comfyui_draw` / `comfyui_img2img` 的 `external=True`
  契约（v7.7.58+）；`source` 只作审计标识；`raw_prompt=True` 直接使用库里精修的
  英文 danbooru tag。**需要生图而萌绘不可用 → 报错不出卡，不做无图降级**
  （立绘即产品本体）。
- **策略分派**：`base`（底模原生认识的角色 tag）/ `lora`（走萌绘 loras 参数）/
  `img2img`（官方立绘参考图 + denoise 0.65，覆盖底模不认识的冷门角色）。
- **角色库**：不打包死库——包内仅 20 条种子卷（动漫 8 / gal 4 / 游戏 8），首启复制到
  `data/plugin_data/astrbot_plugin_moe_waifu/pools/`，此后 data 目录唯一真源；
  分卷分类（anime/gal/game/other）；`group_pools` 分群绑定卷；只有 verified
  角色参与抽卡；`/老婆试画`（管理员）过渡精修端点。
- **卡片**：纯 Pillow 700×1200 竖版，立绘全幅打底 + 角色应援色渐变遮罩 + 白字信息面板
  （名字楷体 / 分类与稀有度徽章 / 印象语 / 今日同款 N 人 / 第 N 次抽到她 / 重抽标）；
  缺立绘拒绝渲染。
- **存储**：SQLite 五表（draws / user_char_counts / char_stats / portraits /
  draw_jobs），同步 sqlite3 + 公开方法串行化（星语同款）；卡图 90 天自动清理。

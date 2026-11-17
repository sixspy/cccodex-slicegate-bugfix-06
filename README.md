# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10631 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 关日门闩（已选定，口径甲）

关日门闩说明见 `docs/关日门闩说明.md`，代码内 `GATE_MODE` 与本文件一致，选定后当天不许更换。

- **甲门闩（本仓口径）**：关日后该业务日一切放行与水位推进硬拒绝，仅允许只读查询。
- 乙门闩（迟到冻结区登记）不采用；任何乙口径写入口保留但一律拒绝并记“关日口径偏了”。
- 关日状态只认服务端 `day_closes` 表，页面不维护本地关日标记。
- 同业务日关日与放行排队互斥，后手记“关日争用”并拒绝，禁止默认成功。
- 关日列表 / 业务目录 / 切片侧栏 / 策略旁注 / 放行流水由同一次 `/api/snapshot` 快照读出，关日画面与放行流水读数逐日对账。

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-bugfix-06
docker compose up -d --build
`

浏览器：http://127.0.0.1:10631/  
健康检查：GET http://127.0.0.1:10631/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10631
python -m pytest -q
`

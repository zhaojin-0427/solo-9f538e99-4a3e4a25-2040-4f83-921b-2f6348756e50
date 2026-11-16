# 研究影像库 DICOM UID 隔离转换平台

多中心影像数据并入研究影像库时，按医院隔离重复 UID、保留跨批引用的 Web 服务。
基于 **Flask + pydicom + SQLite**，以 Docker Compose 一键启动。

## 功能与业务规则

| 主题 | 实现方式 |
| --- | --- |
| 输入 | 浏览器填写医院代号（2–20 位字母/数字/`_`/`-`），每批最多 10 份**未压缩 CT** DICOM |
| 接受的传输语法 | `1.2.840.10008.1.2` 隐式 VR LE、`1.2.840.10008.1.2.1` 显式 VR LE、`1.2.840.10008.1.2.2` 显式 VR BE；压缩语法或非 CT 整批拒绝 |
| UID 重写 | 检查（Study）、序列（Series）、实例（SOP Instance，含文件头 `MediaStorageSOPInstanceUID`）、参考坐标系（Frame of Reference）以及全部嵌套序列中的实体/引用 UID |
| 保持不变 | SOP 类 UID、传输语法 UID、实现/创建者/编码方案等类 UID（见 `app/uidmapper.py` 中 `NON_REMAPPED_UI_KEYWORDS`），以及 **PixelData 像素字节**（保存后重新读盘逐字节校验） |
| 新 UID 合法性 | `2.25.<128 位整数>`（DICOM 标准的 UUID 派生 UID，无需申请机构前缀），确定性派生：`UUIDv5(固定命名空间, 医院代号 + 原 UID)`，长度 ≤64、组件规则全部校验 |
| 跨批/重启不变 | 映射按 `(医院, 原 UID)` 持久化于 SQLite；同院同原 UID 永远得到同一新 UID，重启后依然成立 |
| 未入库引用 | 文件中出现但对应实体不在本批的引用 UID（如 `ReferencedSOPInstanceUID`、`ReferencedFrameOfReferenceUID`）同样计算并预留映射，后续批次自动收敛为同一值 |
| 医院隔离 | 医院代号参与派生输入，不同医院同一原 UID 得到不同新 UID；冲突判定也按医院分别进行 |
| 同字节重传 | 同院、同 SOP UID、同 SHA-256 重传 → 复用并返回原成品文件（仍登记新批次），不产生重复成品 |
| 异字节冲突 | 同院、同 SOP UID、不同字节 → **整批拒绝**（HTTP 302 + 页面错误提示），不写任何成品/映射 |
| 并发安全 | 进程内 `threading.RLock` 串行化发布 + SQLite `BEGIN IMMEDIATE` 写锁 + 映射唯一约束，并发上传不会出现两套映射 |
| 原子发布 | 成品先写 `staging/batches/<批次号>/`，单事务提交登记，再 `rename` 原子移动到 `published/`；失败回滚并清理暂存 |
| 崩溃恢复 | 启动时清空 `staging/batches/` 中未完成批次；只开放 `status='published'` 的完整批次，已发布成品重启后仍可下载 |
| 浏览器界面 | 上传表单、批次详情（旧→新 SOP 对照、逐文件下载）、按医院查看全部 UID 引用对应（含未入库预留映射）、**检索配置/重建/影像检索页（`/search`）** |

## 数值标签影像检索（扫描参数挑片）

影像数据员按研究扫描参数挑片时使用，全部操作在浏览器 `/search` 页完成。

| 主题 | 实现方式 |
| --- | --- |
| 检索配置 | 配置 1～3 个**顶层标准数值标签**（VR ∈ DS/IS/FL/FD/SS/US/SL/UL 且 VM=1，如 `SliceThickness`、`KVP`）；未知标签、私有标签、类 UID/字符串/序列/多值标签在提交时即拒绝并提示 |
| 检索方式 | 选择医院，每个字段给出闭区间上下限（可只填一端）；SQL 以 REAL 数值比较，命中按 `(医院, 原 SOP)` 唯一索引去重，结果展示**原 SOP Instance UID、各字段数值与成品下载链接**（稳定链接 `/products/<batch_file_id>`） |
| 不命中规则与说明 | 标签缺失、空值、非数值、非有限数值（NaN/无穷大）或多值均不进索引；每个成品按首个失败字段写入 `search_exclusion` 并在页面逐条说明原因（读取失败同样列明） |
| 重建与切换 | 修改配置即插入新版本（`building`）并触发后台全量重建；旧配置在重建完成前继续检索。全部已发布成品处理完后，在单事务内把旧版本置 `superseded`、新版本置 `active`，**配置与索引整体原子切换** |
| 旧表单拒绝 | 检索表单携带生效版本号；切换后旧版本号提交会被拒绝并提示重新检索 |
| 重建期间可用性 | 扫描在后台线程分块提交、不持长写锁；上传与下载全程可用。上传在同一批次事务内对生效版本增量建索引，重建收尾再在批次锁内做一次覆盖补齐，因此扫描前已开始及扫描期间上传的成品都不会漏入新索引 |
| 复用/失败语义 | 同字节重传复用既有成品，不产生重复索引；上传失败随批次事务回滚，不留任何索引痕迹 |
| 失败/中断/重启 | 重建失败或进程中断时旧版本继续生效，失败原因保留在版本行；重启后自动重新排队（部分索引先清空），也可在页面手动重试。迟到的旧重建在切换前检查更新版本，存在更新的生效版本则直接作废，不能覆盖最新配置 |
| 既有卷 | 新表通过 `CREATE TABLE IF NOT EXISTS` 幂等迁移；UID 映射表与成品字节完全不参与重建，已有卷重启后直接可用 |

### 检索相关数据表

- `search_config`：配置版本（`building` / `active` / `superseded` / `failed`）、字段 JSON、统计与错误信息。
- `search_index`：`(config_id, hospital_id, sop_uid_old)` 唯一，字段值以 REAL 存储（`value_1..3`）。
- `search_exclusion`：未命中成品及首个失败字段/原因，`(config_id, batch_file_id)` 唯一。

## 目录结构

```
app/
  __init__.py        # Flask 工厂、目录初始化、启动恢复、检索重建后台线程
  config.py          # 路径/限制/允许的传输语法
  db.py              # SQLite schema 与连接管理（含检索三张表）
  uidmapper.py       # 确定性 UID 派生、合法性校验、改写/保留清单、分类
  dicom_engine.py    # pydicom 读取校验、UI 元素收集（含 file_meta/嵌套序列）、改写、像素校验
  search.py          # 检索字段校验、数值抽取、后台重建/原子切换、闭区间查询
  services.py        # 批次编排：冲突检查、映射落库、暂存→原子发布、生效索引增量登记、查询
  routes.py          # HTTP 路由（上传/批次/映射/下载 + 检索配置/重建/检索/成品链接）
  templates/         # 上传页、批次页、映射对照页、检索页
tests/               # 54 项 unittest（单元 + 引擎 + 端到端 + 边界 + 检索 23 项）
Dockerfile
docker-compose.yml
wsgi.py              # gunicorn 入口
```

## 启动

```bash
docker compose up --build
# 浏览器访问 http://localhost:8000
```

数据（SQLite、暂存区、已发布成品）保存在命名卷 `dicom-data`（容器内 `/data`），
重启容器后映射与成品均保留。

### 本地（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DICOM_DATA_DIR=./storage gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 wsgi:app
```

> 部署上固定 **1 个 gunicorn worker + 多线程**：进程锁在单进程内串行化批次发布，
> 读请求与下载仍并发；SQLite WAL 支持并发读、单写。

## 自测

```bash
python -m unittest discover -s tests -v
```

覆盖：UID 合法性/确定性/医院隔离、类 UID 与传输语法保留、像素字节不变、
隐式/显式/大端三种语法、非 CT 与压缩语法拒绝、多值 UI、同字节重传复用、
异字节整批冲突、跨批与重启映射稳定、医院互不混用、8 路并发无重复映射、
崩溃暂存清理、页面与下载 200/404；以及检索字段校验（数值型/单值/顶层标准）、
缺失/空值/非数值/NaN/多值五类不命中、闭区间检索与 SOP 去重、
重建原子切换与旧表单拒绝、扫描期间上传不漏入、失败重试、重启恢复、
迟到旧重建无法覆盖新配置、既有卷 UID 与成品字节不变。

## 关键设计说明

- **为什么派生中包含医院代号**：保证不同医院命名空间互不混用；同院内则由
  `(hospital_id, old_uid)` 唯一约束保证一对一，`new_uid` 全局唯一约束兜底碰撞。
- **为什么私有/未知 UI 标签不改写**：私有 UID 的语义无法从标准字典确认，
  仅改写 pydicom 字典中 VR=UI 且不在保留清单内的标准标签，避免破坏私有数据。
- **冲突检测先于任何写入**：所有文件先在请求工作区读盘建计划，确认无 SOP/字节
  冲突后才开启事务与暂存目录，因此冲突批次不会留下任何痕迹。

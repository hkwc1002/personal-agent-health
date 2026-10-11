<div align="center">

# personal-agent-health

**深度融入个人生活助手的智能健康管理模块**

打通飞书私聊交互、多模态拍照识餐、华为穿戴设备生理数据与本地营养账本，为个人 Agent 提供确定性的健康数据底座与动态建议能力。

[English](README.md) · **简体中文**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker Compose v2](https://img.shields.io/badge/docker--compose-v2-blue.svg)](https://docs.docker.com/compose/)

[核心特性](#核心特性) · [系统架构与数据联动](#系统架构与数据联动) · [快速上手](#快速上手) · [数据与隐私策略](#数据与隐私策略) · [安装文档](docs/INSTALL.md)

</div>

---

## 项目定位

`personal-agent-health` 不是单纯的 LLM 提示词模板或问答 Demo，而是运行在个人基础设施上的**私有健康服务模块**。

本项目作为插件集成进现有 Agent 运行时（当前适配 Hermes Agent），将零散的健康生活管理闭环到同一条工作流中：
- 解决纯大模型做健康管理时的“幻觉计算”与“上下文遗忘”问题：营养素计算、时序趋势与配额结余全部由 Python 确定性逻辑和 SQLite 账本承载；
- 解决穿戴设备数据孤岛问题：通过 Intervals.icu 自动化管道拉取华为手表/手环采集的真实运动与生理负荷指标；
- 贴近真实日常交互：依托飞书私聊窗口，发送食物照片即完成多模态成分分析与账本入库，支持随时以自然语言纠正份量。

---

## 核心特性

### 1. 飞书日常对话与私聊交互
- **即时记录**：在飞书对话框直接发送食物照片、文字或语音（如“今天体重 68.5kg”），Agent 自动调用底层工具入账。
- **对话式单据修订**：支持语义级追溯修改（如“刚才的米饭只吃了一半”或“取消刚才那餐”），服务执行版本校验并保留修订历史，不产生重复流水。
- **隐私边界隔离**：插件在网关层严格拦截群聊调用。即使在群组中 @ 机器人，健康工具同样拒绝执行，确保个人健康档案与流水仅在私聊生效。

### 2. 多模态饮食分析与结构化提取
- **视觉多模态识别**：调用视觉模型识别餐食构成，拆解混合菜品食材与预估克重。
- **精细化营养素结构化提取**：
  - 核心宏量营养素：热量（`kcal`）、蛋白质（`protein_g`）、脂肪（`fat_g`）、碳水化合物（`carbs_g`）；
  - 关键健康风险指标：独立区分总糖（`sugars_g`）与**游离糖/添加糖（`added_sugars_g`）**，并记录钠（`sodium_mg`）与膳食纤维（`fiber_g`）。
- **区间估算与未知保留**：对餐馆混合菜肴采用 `[low, best, high]` 三值区间存储。无法肉眼判定的隐形烹调油、添加糖及调味品明确标注为未知项，拒绝虚假的高精度猜测。
- **双重防重保障**：图片传输计算 SHA-256 哈希去重，结合会话消息 ID 幂等控制，避免重发图片导致的重复记账。

### 3. 关键数据联动：华为穿戴设备自动同步（亮点）
- **官方授权链路**：通过 `华为运动健康 -> Intervals.icu -> personal-agent-health` 数据链路，无需每天手动截图或导出原始文件。
- **运动与负荷抓取**：自动抓取跑步、骑行、游泳、力量训练等活动的运动类型、实际时长、距离、平均心率、最大心率及报告热量。
- **全天生理指标入库**：定时同步步数、睡眠时长、睡眠评分及静息心率（`resting_hr_bpm`）。
- **增量拉取机制**：后台线程每小时自动回查最近 7 天数据，初次配置回查 30 天，网络中断后自动补齐缺口。
- **理性消耗核算**：设备上报的运动消耗独立存储展示，不直接盲目计入饮食“可多吃配额”，防范运动手表的能耗高估陷阱。

### 4. 动态个性化建议系统
- **身体动态基准档案**：记录年龄、身高、体重、腰围、目标体征、偏好忌口及疾病损伤限制。
- **下一餐动态推导**：调用 `next_meal` 与 `preview` 动作时，Agent 综合当日已摄入总量、剩余宏量营养素额度与近期华为运动负荷，结合 USDA 离线食物库为用户推荐具体食谱。
- **周期健康复盘**：结合一周体重走势、日均步数、睡眠质量和宏量营养素达标率，生成多维度的周度趋势评估。

---

## 系统架构与数据联动

整个系统采用轻量宿主/容器分离模式运行。健康服务独立驻留在轻量侧车容器中，不暴露任何公网端口，仅通过只读/受控的 Unix Domain Socket 响应本地 Agent 进程请求。

```mermaid
flowchart TD
    subgraph 交互终端与设备层
        User["用户 (User)"]
        Watch["华为手环 / 智能手表"]
        Watch -->|蓝牙同步| HuaweiApp["华为运动健康 App"]
    end

    subgraph 外部数据平台
        HuaweiApp -->|平台授权同步| Intervals["Intervals.icu (开放 API)"]
        USDA["USDA FoodData Central / 离线食物库"]
    end

    subgraph Agent 网关层
        User <-->|飞书私聊消息 / CLI| Hermes["Hermes Agent (主网关)"]
        Hermes <-->|视觉调用 / 意图识别| VisionLLM["多模态大语言模型"]
        Hermes <-->|加载插件| Plugin["personal_health 适配层<br/>(身份校验 · 消息防重 · 图像哈希)"]
    end

    subgraph 本地健康服务 [Docker Sidecar / 本地独立进程]
        Plugin <-->|Unix Domain Socket| Socket["/run/personal-agent-health/health.sock"]
        Socket <--> Server["Python API 服务 (无公网端口)"]
        
        Server <--> Engine["营养计算与修订引擎 (health.py)"]
        SyncWorker["后台同步线程 (intervals_sync.py)"] -->|定时轮询 (每小时)| Intervals
        SyncWorker --> Engine
        
        Engine <--> SQLite[("SQLite 数据库 (WAL 模式)<br/>饮食 · 运动 · 身体指标 · 版本快照")]
        USDA -.->|安装阶段离线导入| Engine
    end
```

### 工具集职能划分

| 工具名称 | 支持动作 (Actions) | 职能描述 |
| :--- | :--- | :--- |
| `health_record` | `record` | 接收菜品结构化成分、估算份量范围及图片哈希，写入饮食日志。 |
| `health_query` | `today`, `preview`, `next_meal`, `trend`, `week`, `food_search`, `export` | 读取当日累计、试算候选餐单营养素、提取穿戴设备生理摘要、生成周度趋势数据。 |
| `health_manage` | `profile`, `targets`, `amend`, `weight`, `workout`, `complete`, `erase` | 录入身体档案、设定营养素目标、单据历史版本原子修订、手动补录体征。 |

---

## 快速上手

### 1. 环境准备
- 操作系统：Linux（x86_64 / arm64，建议 Ubuntu 22.04+ 或 Debian 12+）
- 基础组件：Python 3.11+、Docker Engine 与 Docker Compose v2
- 前置依赖：已就绪的 Hermes Agent 实例及其飞书通讯渠道配置

### 2. 部署健康服务侧车

克隆仓库并生成针对当前机器环境的安装计划：

```bash
git clone __REPOSITORY_URL__.git
cd personal-agent-health

# 示例：针对宿主机运行的 Hermes 生成配置计划
python3 scripts/install.py --mode host \
  --hermes-home /home/ubuntu/.hermes \
  --hermes-uid 1000 --hermes-gid 1000 \
  --destination /opt/personal-agent-health \
  --owner-id "ou_xxxxxxxxxxxxxxxx" \
  --timezone Asia/Shanghai
```
> 注：`--owner-id` 为你在飞书系统中的 Open ID，仅允许该用户使用健康工具。

确认输出参数无误后，追加 `--apply` 执行安装：
```bash
sudo python3 scripts/install.py --mode host \
  --hermes-home /home/ubuntu/.hermes \
  --hermes-uid 1000 --hermes-gid 1000 \
  --destination /opt/personal-agent-health \
  --owner-id "ou_xxxxxxxxxxxxxxxx" \
  --timezone Asia/Shanghai \
  --apply
```
安装程序将构建独立健康容器，下载并导入官方 USDA 离线食物营养成分库，并在指定目录下启动 Unix Socket 服务。

### 3. 启用 Hermes 插件
切换至运行 Hermes 的操作系统用户，验证并加载插件：
```bash
hermes plugins validate /home/ubuntu/.hermes/plugins/personal-agent-health
hermes plugins enable personal-agent-health
```
重启 Hermes 网关服务使插件生效。

### 4. 接入华为手表 / Intervals.icu 数据同步（可选）

1. **配置华为健康数据流出**：
   - 登录 [Intervals.icu](https://intervals.icu/) 网页端。
   - 打开 `Settings` -> `Connections`，选择华为运动健康（Huawei Health）并发起授权连接，跟随页面提示完成华为账号登录与读取权限授予。
   - 确认 Intervals.icu 的日程表页面中已成功显示近期的华为手表运动记录。
2. **获取 API 凭证**：
   - 在 Intervals.icu 的 `Settings` -> `Developer Settings` 中记录你的 `Athlete ID`，并生成专用的 `API Key`。
3. **安全注入凭证**：
   在服务器终端运行配置脚本，隐式输入上述密钥（凭证保存在本地受限文件中，不经过聊天渠道）：
   ```bash
   sudo python3 scripts/configure.py intervals --destination /opt/personal-agent-health
   ```
4. **生效同步**：
   重启健康侧车容器后，同步线程将立即启动并周期性拉取穿戴数据：
   ```bash
   docker compose -p personal-agent-health -f /opt/personal-agent-health/compose.generated.json restart health
   ```

---

## 常用交互示例（飞书私聊）

完成配置后，在飞书客户端私聊机器人即可直接对话：

```text
# 1. 建立初始档案
你：帮我建立健康档案，身高 178cm，体重 73kg，日常久坐，每周去两次健身房，目标减脂到 68kg。
Agent：[调用 health_manage profile/targets，完成基础代谢计算并设定每日蛋白质/热量预算]

# 2. 拍照记餐与多模态分析
你：[发送一张午餐盖码饭的照片]
Agent：午餐已记录：预计热量约 650 kcal（580–740 kcal），含蛋白质 28g、碳水 85g、游离糖约 4g。烹调油按外卖标准估算；若实际未吃完可随时修正。

# 3. 语义化份量纠偏
你：刚才米饭剩了三分之一，另外喝了一罐无糖可乐。
Agent：[调用 health_manage amend 原子更新原记录] 已调整刚才的午餐流水：扣减碳水约 18g，增加无糖饮料记录。当前修正版本为 v2。

# 4. 结合穿戴设备数据的下一餐指导
你：看下手环数据，今天消耗怎样？晚餐推荐吃什么？
Agent：[调用 health_query today 拉取穿戴设备及饮食摘要] 
华为手表数据显示：今日已完成晨跑 5.2 公里（均心率 142 bpm），步数 8,430 步，睡眠评分 82 分。
当日热量预算尚余 580 kcal，蛋白质缺口仍有 35g。
建议晚餐：香煎鸡胸肉或清蒸鱼 150g + 水煮西兰花 200g + 杂粮饭半碗。
```

---

## 数据与隐私策略

- **私有文件驻留**：所有的身体指标、饮食单据、版本修改记录均存储在宿主机本地的 SQLite 数据库（`health.sqlite3`）中。
- **免除二次上传**：健康侧车不转存任何照片原图；图像仅在 Agent 上下文中交由使用者配置的视觉模型推理。
- **严格最小权限**：服务运行在专属隔离 UID 下，仅通过文件系统属组权限与 Unix Socket 建立管道，拒绝暴露 TCP/IP 外部端口。
- **数据自主掌控**：支持随时通过 `health_query export` 导出完整 JSON 数据，或使用 `health_manage erase` 彻底清空本地所有流水。

---

## 许可证

本项目依据 [MIT 许可证](LICENSE) 开源发布。所引用的第三方数据集（USDA 等）遵循各数据源自身许可证约束，详见 [THIRD_PARTY.md](THIRD_PARTY.md)。

# 安装 personal-agent-health

这份文档同时面向使用者和代为安装的 AI。目标是收到一行安装请求后，AI 能自行完成环境检查、安装、插件激活和验收；只在账号登录、平台授权、隐藏输入密钥或必要系统权限确认时交给使用者。

## 安装范围

支持单用户 Linux、Python 3.11+、Docker Engine 和 Compose v2。Hermes 可运行在宿主机或 Docker Compose 中。首版不支持 rootless Docker、user namespace 映射、Kubernetes 和直接运行在 Windows/macOS 的服务。

使用已有 Hermes、模型和飞书配置。没有 Hermes 时，先按[官方文档](https://github.com/NousResearch/hermes-agent)建立可用对话环境；安装器不会自动购买模型服务或注册飞书应用。

AI 应先阅读本仓库源码和本文件。执行所在环境的审批规则始终有效，不自动回答安全扫描的警告或强制放行。不要读取或打印现有 `.env`、认证令牌、整份 Docker inspect/config 输出、健康数据库和会话正文来“了解环境”。

## 1. 定位环境

从用户给出的仓库链接下载固定提交，记录提交号。若直接下载源码包，记录来源及校验值；不要把命令中的示例用户名当成真实仓库。

只读取安装所需的元数据：

1. 当前 Hermes 版本/提交、实际 profile 的 `HERMES_HOME`、运行 UID/GID。不要根据用户名猜 UID。
2. `hermes plugins --help`，确认支持 `validate`、`enable` 和官方 `register(ctx)` 插件入口。
3. 若为容器，确认其 Compose 项目名、服务名、所有原始 Compose 文件，以及 profile 的宿主机持久化路径和容器内路径。可以定向读取容器 labels、mounts、Config.User，不能把包含全部环境变量的 inspect 发进聊天。
4. 当前 profile 是否已有 `health_record`、`health_query`、`health_manage`。已有旧健康集成时停止新装，提出迁移方案，避免覆盖旧账本或重复注册。
5. 确认 `state.db` 存在；只用 `PRAGMA table_info` 检查 `messages(session_id, platform_message_id, role, content)` 和 `sessions(id, source, user_id)`，不读取真实对话。
6. 确认图片缓存实际位置。默认 `<HERMES_HOME>/cache` 和 `image_cache`；不同布局需调整生成的插件配置后再激活。
7. 飞书本人 ID 从已知配置或可信的私聊身份信息获取。插件使用独立的 `owner_id`，不把机器人整个群聊白名单当健康授权人。

需要新装 Docker、提权或重新创建正在服务的 Hermes 容器时，先说明对目标实例的影响。若安装请求已覆盖该操作则继续，否则遵守工具要求取得授权。首次接入容器挂载需要一次重建目标 Hermes 服务。

## 2. 生成安装计划

从源码根目录运行。以下路径和 UID 是示例，AI 必须替换为上一步实际值。

宿主机 Hermes：

```bash
python3 scripts/install.py --mode host \
  --hermes-home /home/alice/.hermes \
  --hermes-uid 1000 --hermes-gid 1000 \
  --destination /opt/personal-agent-health \
  --timezone Asia/Shanghai
```

Docker Compose Hermes：

```bash
python3 scripts/install.py --mode docker \
  --hermes-home /srv/example-hermes/data \
  --hermes-container-home /opt/data \
  --hermes-uid 1000 --hermes-gid 1000 \
  --compose-file /srv/example-hermes/compose.yaml \
  --compose-project example-hermes --hermes-service agent \
  --destination /opt/personal-agent-health \
  --timezone Asia/Shanghai
```

使用飞书时追加 `--owner-id 本人飞书ID`。不提供时默认拒绝飞书健康访问，但保留本地 CLI。有多个原始 Compose 文件时，按原有顺序重复传 `--compose-file`。不能省掉覆盖文件、换项目名或用新模板替换整个现有部署。

本步骤不创建文件，也不启动容器。核对输出中的安装路径、UID/GID、目标服务和时区。选定的 `--service-uid` 默认 21000，必须与 Hermes UID 不同；检查该 UID 没有被宿主机其他敏感服务占用，冲突时换一个专用 UID。

## 3. 安装健康侧车

以 Linux root 权限运行相同命令，追加 `--apply`。需要时由使用者在系统终端完成 sudo 验证。

安装器会：

- 将健康代码放在独立的安装目录；将最小插件放到当前 profile 的 `plugins/personal-agent-health/`。
- 创建独立 UID 持有的 `data/`、`secrets/` 和 socket 目录；仅挂载 socket 和插件配置给 Hermes。
- 构建健康镜像，从 USDA 下载公开离线食品数据，按实际单位校验后导入空账本的食品缓存。
- 启动健康侧车并等待健康检查，输出后续插件激活命令。

代码不含个人账本。不要复制作者或其他使用者的 `health.sqlite3` 来完成安装。

网络无法下载 USDA 时保留安装目录，排查公共数据端点后重试食品导入和启动命令。明确接受只有标签/未知营养记录时，新安装可加 `--skip-foods`；报告“营养库未导入”，不能声称营养估算链路已通过。`DEMO_KEY` 可能限流，不能作为完整离线库的替代保证。

初次安装器拒绝覆盖已有安装，避免把升级误当新装。中途失败时读取自己生成的 `install-state.json`，继续失败的 build/import/up 步骤；不要删除 `data/` 或重新复制整个 profile。命令为：

```bash
docker compose -p personal-agent-health -f /opt/personal-agent-health/compose.generated.json build health
docker compose -p personal-agent-health -f /opt/personal-agent-health/compose.generated.json run --rm --no-deps --entrypoint python health -B /app/import_foods.py
docker compose -p personal-agent-health -f /opt/personal-agent-health/compose.generated.json up -d --wait health
```

上面的路径同样要随实际 `--destination` 替换。

## 4. 激活 Hermes 插件

**宿主机 Hermes：**在实际 Hermes 用户及 profile 的环境里执行安装计划返回的 `hermes plugins validate` 和 `hermes plugins enable`。不要在 root 的另一个 profile 中启用。随后用现有运行方式重启该 Hermes 网关或开启新 CLI 会话，使插件生效。

**Docker Compose Hermes：**按安装计划返回的 `activation_commands` 顺序执行。生成的 `compose.override.json` 只增加目标服务所需的插件、非密钥配置和 socket 挂载：

1. `config --quiet` 校验原始文件与新增覆盖文件组合。
2. `up -d --no-deps <目标服务>`，保留原项目名与全部原始 Compose 文件。
3. 在该容器中执行 `hermes plugins validate <插件目录>` 与 `hermes plugins enable personal-agent-health`。
4. 重新加载目标 Hermes 服务。不要重启其他服务。

后续操作这个 Compose 项目时必须继续带上新增覆盖文件，或将相同挂载按原有部署规范合并到配置中。不要用不包含覆盖文件的 `up` 意外移除健康挂载。

插件工具集是 `personal_health`。若原 Hermes 明确限定可用工具，在目标渠道启用这个工具集，保留其他工具选项。插件发现成功不等于当前模型已经看到工具。

## 5. 华为手环／手表自动同步

这是项目的重点接入流程；不使用穿戴设备的用户可以跳过。

1. 使用者确认华为手环／手表已绑定华为运动健康，手机 App 中可以看到近期设备记录，并已完成平台要求的云端同步。
2. 使用者登录 Intervals.icu，在连接页面发起华为授权。华为登录、授权范围选择和验证码由本人完成，AI 不收集账号密码。
3. 授权后检查 Intervals.icu 中是否已经出现近期华为运动记录，以及实际可用的日摘要字段。仅看到“已连接”不能判定数据已经同步。
4. 生成自己的 Intervals.icu API Key，再通过下方本地工具隐藏输入。

接入路线是“华为设备 → 华为运动健康 → Intervals.icu → 健康账本”。本项目调用 Intervals.icu 的读取接口，不直接访问华为账号。能否授权及同步哪些字段，以实际设备、账号地区、平台支持和返回结果为准；找不到华为连接或没有数据时报告该步骤未完成，不宣称支持所有型号。其他设备只有在 Intervals.icu 已提供可用数据时才沿用后续读取流程。

然后在使用者自己的终端运行隐藏输入工具：

```bash
sudo python3 scripts/configure.py intervals --destination /opt/personal-agent-health
```

程序会询问 athlete ID，再隐藏输入 API Key。AI 不应截取终端键盘输入、剪贴板、密钥弹窗或 credential 文件内容。程序只报告是否写入成功，不自动验证账号。

配置后只重启健康侧车：

```bash
docker compose -p personal-agent-health -f /opt/personal-agent-health/compose.generated.json restart health
```

首次同步回查30天。调用 `health_query` 的 `status`、`trend`，确认实际 `last_success_at`、记录时间范围和可用字段；有最后同步时间但没有记录，只能说明接口访问成功，不能声称手环已有数据。

使用华为接入时，还应核对运动条目的实际来源标识。日摘要未提供单指标来源时，只称为 Intervals.icu 返回的健康摘要，不把步数、睡眠、体重等所有字段都标成华为测量。再次同步应保持相同记录数，上游内容变化则修订原记录；未收到的睡眠或心率保留未知。

可选 USDA 在线 Key 用 `sudo python3 scripts/configure.py usda --destination ...` 隐藏输入。离线食品库不需要 Key，导入后可以先完成基础功能。

## 6. 验收

按顺序检查并报告每项实际结果：

| 检查 | 通过条件 |
| :-- | :-- |
| 合成业务测试 | `python3 -B scripts/test.py` 通过；真实账本没有测试餐 |
| 侧车与权限 | 健康检查通过；无公开端口；Hermes 未挂载健康 `data/` 或 `secrets/` |
| 插件加载 | `validate` 通过，`/plugins` 或实际工具目录显示三个健康工具与 Skill |
| 真实工具读取 | 通过 Hermes 调用 `health_query {"action":"status"}`，得到服务结果 |
| 食品库 | `food_search` 查询 `rice cooked` 得到有来源的食品，`preview` 能确定性计算 |
| 飞书身份 | 本人私聊可查询，其他用户及群聊被拒绝；不能只测 socket 健康检查 |
| 图片链路 | 使用者发送一餐真实照片后记录成功，重复同文件不重复入账，纠正更新原记录 |
| 华为穿戴（若启用） | 上游有实际华为来源运动，账本收到对应记录及可用日摘要；重复同步不重复，缺失字段不记零。只有授权成功或接口可访问不算完整通过 |

图片步骤会调用使用者所选模型并产生其正常费用，只在其授权使用后执行。不能用伪造的个人测试餐写入正式账本。安装期间可先做隔离合成测试，首餐由使用者发送；未完成时标注“图片链路待首餐验证”。

## 升级、备份与撤销

源码和数据分开。升级前使用 `health_manage backup` 获取一致性 SQLite 快照，并备份插件配置和部署文件；对照发行说明升级 `app/` 与插件代码，仅重建健康服务、重新加载目标 Hermes。不要重新执行初装覆盖数据。

可在健康侧车内用 `Health('/data').backup()` 创建快照。活动中的 SQLite 不能只拷贝主文件忽略 WAL；离机备份使用一致性快照并自行加密。`data/backups/` 中存在的历史快照同样包含健康信息。

撤销安装时，先在原 profile 执行 `hermes plugins disable personal-agent-health`；只移除本插件的挂载并重新加载目标 Hermes，再停止健康侧车。保留 `data/` 和 `secrets/`，明确决定是否清除后再处理。不要删除整个 Hermes home 或其他服务。

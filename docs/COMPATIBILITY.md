# 兼容性与验证范围

首版是 Alpha，不能将原个人部署成功等同于通用安装器已经在所有环境通过。

## 源码接口基线

2026-10-11 对照 Hermes 官方 `main`，记录提交：

`b65cd56cfcdc33cbf6657326b5711a7c23cd1485`

采用：

- `plugin.yaml` 与 `register(ctx)` 入口。
- `ctx.register_tool()` 和 `ctx.register_skill()`。
- `gateway.session_context.get_session_env()` 读取可信会话标识。
- `hermes plugins validate` 与 `hermes plugins enable`。

参考：[官方插件文档](https://github.com/NousResearch/hermes-agent/blob/b65cd56cfcdc33cbf6657326b5711a7c23cd1485/website/docs/user-guide/features/plugins.md)、[会话上下文源码](https://github.com/NousResearch/hermes-agent/blob/b65cd56cfcdc33cbf6657326b5711a7c23cd1485/gateway/session_context.py)。

这个提交用于接口核对，不是已执行完整安装验收的声明。其他版本需先通过安装文档中的兼容性检查。

## 已保留的运行设计

核心源自单用户健康侧车：SQLite、营养换算、照片去重、版本化修订、体测、运动和 Intervals.icu 同步。公开版将路径、时区和授权人配置化，通过官方插件注册工具。

插件仍依赖 Hermes `state.db` 的会话表结构验证飞书图片归属。若上游字段或缓存布局改变，应更新适配器；不要为了安装成功删除图片归属校验。

## 验证层级

| 层级 | 方法 | 不能证明的事项 |
| --- | --- | --- |
| 合成业务测试 | 临时账本中的去重、并发、修订、来源、未知值和设备同步测试 | 真实餐食准确率 |
| 插件契约测试 | 模拟 `register(ctx)`，检查工具和 Skill 注册及身份拒绝行为 | 真实 Hermes 版本已经加载成功 |
| Linux socket 测试 | 临时服务进程、真实 Unix socket 和 peer UID 拒绝 | 目标 VPS 的容器挂载权限 |
| 安装计划测试 | 生成 host/docker 配置，确认只挂载插件、配置和 socket | Docker 已拉取镜像并启动成功 |
| GitHub CI | 执行业务测试、发布检查与 Docker 冒烟 | CI 尚未运行时不能当作通过证据 |
| 使用者环境验收 | 按 INSTALL.md 检查真实工具、本人私聊、图片和可选设备数据 | 其他地区、设备或模型同样可用 |

Windows 上可运行大部分纯 Python 业务测试，但 Linux 专属 socket 和容器路径测试会跳过。发布说明应给出实际运行平台、通过/跳过数量及尚未执行的验证，不把跳过写成通过。

## 本次本地验证

2026-10-11，在 Windows / Python 3.12 环境运行 `python -B scripts/test.py`：83 项中80项通过，3项 Linux 专属测试跳过。覆盖核心账本、工具身份隔离、设备同步、公开食品导入、插件注册契约、安装计划及发布包生成。

`scripts/check_release.py` 检查通过：33 个允许发布文件，没有未列入清单的文件，也未发现检查器覆盖的常见密钥或个人身份特征。该检查不替代发布前对暂存内容的人工审阅。

本机没有可用 Docker 或已安装的 WSL Linux，未执行 Linux socket 测试、Docker 镜像构建或干净 Hermes 环境的实际安装。对应测试与 GitHub CI 已提供，但未运行的项目不计入已通过结果。没有连接或修改现有生产 VPS，也没有调用真实模型或健康账号。

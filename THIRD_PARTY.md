# 工具、数据来源与许可

项目代码使用 MIT。这个许可不改变外部数据、软件、模型和平台的条款。本仓库不分发食品数据包、运动图片、个人记录或第三方密钥。

| 来源 | 使用内容 | 来源与许可边界 |
| --- | --- | --- |
| Hermes Agent | 插件接口、会话上下文接口 | [官方仓库](https://github.com/NousResearch/hermes-agent)、[MIT 许可](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE)。本仓库通过接口集成，不包含 Hermes 发行版 |
| USDA FoodData Central | SR Legacy 2018-04 CSV、营养单位支持表、在线营养条目 | [下载页](https://fdc.nal.usda.gov/download-datasets/)、[API 指南](https://fdc.nal.usda.gov/api-guide.html)。官方说明数据为公共领域并采用 CC0；导入后保留来源、版本和下载 SHA-256 |
| Open Food Facts | 按条码获取的包装食品营养 | [API 文档](https://openfoodfacts.github.io/openfoodfacts-server/api/)、[复用条款](https://world.openfoodfacts.org/terms-of-use)。数据库适用 ODbL；产品内容与图片分别有许可要求。公开缓存或派生数据库前单独核对，不统一标为 MIT |
| wger | API 动作目录 | [项目](https://github.com/wger-project/wger)、[文档](https://wger.readthedocs.io/en/latest/)。软件与动作/图片的数据许可不同，具体条目按返回来源及许可处理，不批量打包媒体 |
| Intervals.icu | 账号授权后的运动与每日健康摘要 | [官方 API](https://intervals.icu/api-docs.html)。不是本项目提供的公共健康数据库，访问权限与使用条款由平台决定；仅导入使用者自己的授权数据 |
| Python / SQLite / Docker 官方基础镜像 | 运行时与数据库 | 在使用者环境运行，分别适用 Python、SQLite、基础镜像及组成软件的许可；仓库不附带二进制运行时 |
| 使用者配置的模型 | 视觉识别与自然语言建议 | 使用各模型服务的条款和价格，本项目不提供统一模型账号 |

README 参考 [Magpie 中文 README](https://github.com/yetone/magpie/blob/main/README.zh-CN.md) 的信息结构，文案为本项目重新编写，未复制其图标、截图或品牌素材。

食品库保留来源信息用于追溯。若要公开导出的食品缓存，先检查其中是否包含个人配方及第三方数据库内容，不能仅因本项目采用 MIT 就将全部缓存按 MIT 再发布。

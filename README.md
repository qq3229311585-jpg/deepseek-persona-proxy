# deepseek-persona-proxy

一个很小的 **OpenAI 兼容接口中转**,放在自己的服务器上,给 DeepSeek 加上「说话风格」。

起因:TablePlus 的 AI 助手可以接 DeepSeek,但没法设置系统提示词,回答又臭又长。所以在中间加一层:客户端把请求发给这个中转,中转在每次请求里塞进自己的提示词、限制思考和回答长度,再转给 DeepSeek,并把结果(含流式输出)原样返回。DeepSeek 的真实密钥只存在服务器上。

```
TablePlus ──HTTPS──▶ nginx /llm/ ──▶ proxy.py(127.0.0.1:8766) ──▶ api.deepseek.com
  (Base URL +                          ├ 校验访问密码
   访问密码)                           ├ 按「模式」换成真实模型 + 思考强度
                                       ├ 塞入性格提示词 + 界面速查
                                       └ 空回答自动重试一次
```

## 两个模式

在客户端的「默认模型」下拉里选,名字就是模式名(定义在 `settings.json`):

| 模式 | 作用 | 工具调用 | 思考强度 |
|---|---|---|---|
| `教学模式` | 给初学者讲概念:默认短答(150~250 字),按需选一种讲解方法 | **去掉**(AI 不能查库,只讲解) | high |
| `行动模式` | 动手做事:需要就去查库、跑 SQL;会改数据的操作先确认 | 保留 | high |

旧的模型名(如 `deepseek-flash`)通过 `aliases` 指向教学模式,所以已有的客户端设置不用改。

## 文件

| 路径 | 说明 |
|---|---|
| `proxy.py` | 中转程序,只用 Python 标准库,监听 `127.0.0.1:8766` |
| `settings.json` | 模式表、别名、单次回答 token 上限、附带的知识文件。**每次请求都会重新读取,改完不用重启** |
| `prompts/persona.txt` | 通用性格(语气、格式克制、先答后问、不懂就问、不编造背景故事…) |
| `prompts/mode-teach.txt` | 教学模式附加说明:篇幅、讲解方法(定义/举例/比喻/对比/一步步/为什么需要/界面指引/小图) |
| `prompts/mode-action.txt` | 行动模式附加说明 |
| `prompts/ui-tableplus.txt` | TablePlus 界面速查(对照官方文档和真实界面整理),只在对话开头附带一次 |
| `deploy/llm-proxy.service` | systemd 单元 |
| `deploy/nginx-llm.conf` | nginx 里加的 `/llm/` 入口 |
| `deploy/env.example` | 环境变量文件样例(真实的不要提交) |
| `scripts/set_llm_key.sh` | 在终端隐藏输入 DeepSeek 密钥,写到服务器 `/etc/llm-proxy/env`(权限 600),并生成访问密码、复制到本机剪贴板 |

## 部署

1. 服务器上放文件:

   ```bash
   sudo mkdir -p /opt/llm-proxy
   sudo cp proxy.py settings.json /opt/llm-proxy/
   sudo cp prompts/*.txt /opt/llm-proxy/
   sudo chmod 755 /opt/llm-proxy /opt/llm-proxy/proxy.py
   sudo cp deploy/llm-proxy.service /etc/systemd/system/
   ```

2. 在本机写入密钥(密钥隐藏输入,不进命令行和聊天记录):

   ```bash
   SSH_HOST=你的服务器别名 bash scripts/set_llm_key.sh
   ```

3. 启动:

   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now llm-proxy
   ```

4. 在某个 HTTPS 站点的 `server` 块里加入 `deploy/nginx-llm.conf` 的内容,`nginx -t` 通过后重载。
5. TablePlus → 设置 → AI 代理 → 添加 Custom LLM (OpenAI-compatible):
   - **Host**:`https://你的域名`(**只写到域名,不要带路径**,TablePlus 会丢掉主机里的路径)
   - **Sub Path**:`/llm/v1`
   - **API Key**:访问密码(`set_llm_key.sh` 已复制到剪贴板)
   - 默认模型选 `教学模式` 或 `行动模式`

## 安全与隐私

- 中转只监听本机;对外只经过 nginx 的 `/llm/` 路径。
- 必须带访问密码(`Authorization: Bearer ...`),错误请求返回 401,短时间内大量失败会被拖慢。
- 每分钟最多 30 次请求;单次回答上限在 `settings.json` 的 `max_tokens_cap`(思考内容也算在内)。
- **不记录对话内容**,日志只有时间、路径、状态码、耗时。
- DeepSeek 密钥和访问密码只在服务器 `/etc/llm-proxy/env`,仓库里没有。

## 几个实现细节

- 性格提示词放在对话最前面,再在最后补一条提醒(模型更看重靠近结尾的指令)。
- 教学模式会删掉请求里的 `tools` 和历史里的工具调用痕迹,AI 就没法去查库。
- DeepSeek 偶尔只思考不说话,回答为空。非流式会自动重试一次;流式在没出现任何正文时吞掉第一次的结束事件,再请求一次。
- DeepSeek 目前提供的模型名是 `deepseek-flash` 和 `deepseek-v4-pro`(旧名 `deepseek-chat` 仍可用);`reasoning_effort` 取值见其接口报错信息。

## 提示词的来源与许可

- `persona.txt` 里关于语气和格式的原则,参考了 Anthropic 公开的 Claude 对话提示词中的相关段落,按中文和这个场景**用自己的话重写**,没有照搬原文。
- `mode-teach.txt` 里的教学方法,改写自 Anthropic 的 `learn` 技能(Apache License 2.0),文件里有出处说明。
- `ui-tableplus.txt` 来自 [TablePlus 官方文档](https://docs.tableplus.com/) 的整理,加上真实界面截图的核对,同样是自己的话。

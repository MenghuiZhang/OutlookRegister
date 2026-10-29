# OutlookRegister  

简体中文 | [English](./readme_en.md)  

Outlook 注册机（camoufox + cloudflare_temp_email 版本）  

- 模拟人类填表操作，自动过按压验证码
- 浏览器固定使用 [camoufox](https://github.com/daijro/camoufox)（反指纹 Firefox）
- 辅助邮箱固定使用 [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email)（Cloudflare Workers 自建临时邮箱）
- 可选 OAuth2 拿 refresh_token

---

### 使用步骤

1. `pip install -r requirements.txt`  
2. `python -m camoufox fetch official/158.0-beta.40`（首次需下载浏览器内核与 GeoIP 库）  
3. 在 `config.json` 中填写代理与 `cf_tempmail` 相关内容  
4. `python main.py`（命令行）或 `python web_console.py`（网页控制台，推荐）  

---

### 配置说明

| 配置项 | 说明 |
| :--- | :--- |
| **`proxy`** | 代理地址，留空直连。注意：自动化浏览器**不会走系统代理**，必须显式指定。IP 质量与成功率高度相关，同一 IP 短时间不宜多次注册。 |
| **`email_suffix`** | `@outlook.com` 或 `@hotmail.com`。 |
| **`bot_protection_wait`** | 反机器人检测等待秒数。 |
| **`max_captcha_retries`** | 验证码最大重试次数。 |
| **`concurrent_flows` / `max_tasks`** | 并发浏览器数量 / 总任务数。 |
| **`camoufox.headless`** | 无头模式，注册任务建议 `false`（无头易被检测）。 |
| **`camoufox.geoip`** | 时区/语言/经纬度/分辨率自动跟随出口 IP，建议 `true`。 |
| **`camoufox.humanize`** | 鼠标移动拟人轨迹，建议 `true`。 |
| **`camoufox.locale`** | 锁定浏览器语言（注册流程依赖中文页面，保持 `zh-CN`；留空则跟随出口 IP）。 |
| **`camoufox.os`** | 指纹 OS。macOS 宿主机保持 `macos`：Windows 身份在 Mac 有头模式下中文字形会错乱（camoufox #706/#708）。 |
| **`camoufox.version`** | 指定运行时版本（如 `158.0-beta.40`）；留空则用 pip 包锁定版本。 |
| **`camoufox.keep_browser`** | 注册结束后保留浏览器窗口，便于人工检查。 |
| **`cf_tempmail.base_url`** | 部署地址，如 `https://xxx.workers.dev`。 |
| **`cf_tempmail.admin_password`** | 部署时设置的 `ADMIN_PASSWORDS`，填写后走管理接口（推荐）。 |
| **`cf_tempmail.site_password`** | 部署时设置的站点密码 `PASSWORDS`，未设置则留空。 |
| **`cf_tempmail.domains`** | 使用的域名，留空则自动获取，逗号分隔多个域名。 |
| **`oauth2.enable_oauth2`** | 注册后自动走 OAuth2 授权拿 refresh_token，结果存 `Results/secured_tokens.txt`。 |

### OAuth2 说明

在 [Azure](https://azure.microsoft.com/) 注册应用并填写 `client_id` 与 `redirect_url`（通常为 `http://localhost:8000`），Scopes 默认包含 Mail.ReadWrite / Mail.Send / offline_access。

### 网页控制台

```bash
python web_console.py            # 默认 http://127.0.0.1:8788
python web_console.py 9000       # 自定义端口
```

任务启停与实时日志 / 配置表单化编辑（自动备份 `config.json.bak`）/ 结果表格查看。

### 结果文件

- `secured_accounts.txt` / `secured_no_token.txt` —— 注册 + 绑定成功
- `secured_tokens.txt` —— 含 OAuth2 token
- `unbound_accounts.txt` —— 注册成功但辅助邮箱绑定失败

### 补充说明

1. macOS (含 Apple Silicon) 注意：ruyipage 无配套 Firefox；camoufox 需锁定 `os: "macos"`，否则 Windows 身份在 Mac 有头模式下中文乱码。  
2. 停止任务时会尝试关闭浏览器；`camoufox.keep_browser: true` 时保留窗口。  

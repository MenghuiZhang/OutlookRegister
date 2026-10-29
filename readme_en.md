# OutlookRegister

[简体中文](./readme.md) | English

Outlook auto-register tool (camoufox + cloudflare_temp_email edition).

- Human-like form filling, automatic press-and-hold CAPTCHA solving
- Browser fixed to [camoufox](https://github.com/daijro/camoufox) (anti-fingerprint Firefox)
- Recovery email fixed to [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) (self-hosted temp mail on Cloudflare Workers)
- Optional OAuth2 refresh token

---

### Quick Start

1. `pip install -r requirements.txt`
2. `python -m camoufox fetch official/158.0-beta.40` (downloads the browser binary and GeoIP database on first run)
3. Fill in the proxy and `cf_tempmail` settings in `config.json`
4. `python main.py` (CLI) or `python web_console.py` (web console, recommended)

---

### Configuration

| Key | Description |
| :--- | :--- |
| **`proxy`** | Proxy URL; leave empty for direct connection. Note: the automated browser does **not** use the system proxy — it must be set explicitly. IP quality is critical; avoid many signups from one IP in a short period. |
| **`email_suffix`** | `@outlook.com` or `@hotmail.com`. |
| **`bot_protection_wait`** | Anti-bot wait time in seconds before submitting. |
| **`max_captcha_retries`** | Max CAPTCHA retry attempts. |
| **`concurrent_flows` / `max_tasks`** | Concurrent browsers / total tasks. |
| **`camoufox.headless`** | Headless mode. Keep `false` for registration tasks (headless is easier to detect). |
| **`camoufox.geoip`** | Auto-align timezone/locale/geolocation/screen size with the exit IP. Recommended `true`. |
| **`camoufox.humanize`** | Human-like cursor movement. Recommended `true`. |
| **`camoufox.locale`** | Lock browser language (the flow relies on the Chinese page; keep `zh-CN`; leave empty to follow the exit IP). |
| **`camoufox.os`** | Fingerprint OS. Keep `macos` on Mac hosts: a Windows identity garbles CJK glyphs in headed mode on macOS (camoufox #706/#708). |
| **`camoufox.version`** | Pin the runtime version (e.g. `158.0-beta.40`); leave empty to use the pip-pinned version. |
| **`camoufox.keep_browser`** | Keep the browser window open after registration for manual inspection. |
| **`cf_tempmail.base_url`** | Deployment URL, e.g. `https://xxx.workers.dev`. |
| **`cf_tempmail.admin_password`** | `ADMIN_PASSWORDS` set during deployment. When filled, the admin API is used (recommended). |
| **`cf_tempmail.site_password`** | Site password (`PASSWORDS`) if set; otherwise leave empty. |
| **`cf_tempmail.domains`** | Domains to use. Leave empty for auto-discovery; separate multiple with commas. |
| **`oauth2.enable_oauth2`** | Automatically perform OAuth2 authorization after registration; tokens saved to `Results/secured_tokens.txt`. |

### OAuth2

Register an app on [Azure](https://azure.microsoft.com/) and fill in `client_id` and `redirect_url` (usually `http://localhost:8000`); default scopes include Mail.ReadWrite / Mail.Send / offline_access.

### Web Console

```bash
python web_console.py            # default: http://127.0.0.1:8788
python web_console.py 9000       # custom port
```

Start/stop tasks with live logs / form-based config editing (auto-backup `config.json.bak`) / results as tables.

### Result Files

- `secured_accounts.txt` / `secured_no_token.txt` — registered + bound
- `secured_tokens.txt` — with OAuth2 tokens
- `unbound_accounts.txt` — registered but recovery email binding failed

### Notes

1. macOS (incl. Apple Silicon): ruyipage has no bundled Firefox; camoufox must pin `os: "macos"`, otherwise a Windows identity garbles CJK glyphs in headed mode.
2. Stopping a task closes browsers; with `camoufox.keep_browser: true` windows are kept open.

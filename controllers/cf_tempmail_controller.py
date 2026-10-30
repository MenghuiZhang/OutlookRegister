import re
import quopri
import random
import time
import email as email_lib
import email.header
import requests
from get_token import get_proxy
from urllib.parse import quote
from utils import log
from .base_controller import BaseEmailProvider


class CloudflareTempMailController(BaseEmailProvider):
    """
    对接 dreamhunter2333/cloudflare_temp_email (Cloudflare Workers 自建临时邮箱)

    两种模式 (config["cf_tempmail"]):
      - admin_password 非空 → 管理员模式: /admin/new_address 建地址, /admin/mails?address= 收件
      - 否则 → 用户模式: /api/new_address 建地址 (要求部署开启 ENABLE_USER_CREATE_EMAIL),
                /api/mails + Bearer jwt 收件
      - 部署设置了站点密码 (PASSWORDS) 时需填写 site_password (x-custom-auth)
      - domains 留空或 auto_domain=true → 不传 domain, 由服务端自动选域
      - random_subdomain=true → 附加 enableRandomSubdomain, 服务端生成随机子域名
    """

    def __init__(self, email, page):
        super().__init__(email, page)

        cfg = self.config.get("cf_tempmail", {})
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.admin_password = cfg.get("admin_password") or ""
        self.site_password = cfg.get("site_password") or ""
        self.domains_cfg = cfg.get("domains") or ""
        # 域名行为策略: 服务端自动选域 / 启用随机子域
        self.auto_domain = bool(cfg.get("auto_domain"))
        self.random_subdomain = bool(cfg.get("random_subdomain"))
        self.max_request_retries = cfg.get("max_request_retries", 5)

        self.mode = "admin" if self.admin_password else "user"
        self.jwt = None

        self.headers = {"Content-Type": "application/json"}
        if self.site_password:
            self.headers["x-custom-auth"] = self.site_password
        if self.mode == "admin":
            self.headers["x-admin-auth"] = self.admin_password

    # ---------- 基础请求 ----------
    def _request(self, method, path, extra_headers=None, **kwargs):
        if not self.base_url:
            raise TimeoutError("[cf_tempmail] 未配置 base_url")

        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)

        last_error = None
        for retry in range(self.max_request_retries):
            try:
                resp = requests.request(
                    method, self.base_url + path,
                    headers=headers, proxies=get_proxy(), timeout=15, **kwargs
                )
                if resp.status_code == 200:
                    return resp
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as e:
                last_error = e
            if retry + 1 < self.max_request_retries:
                log(f"临时邮箱服务请求失败 (第 {retry + 1}/{self.max_request_retries} 次): {last_error}, 重试", "WARN")
            self.page.wait_for_timeout(2000)
        raise TimeoutError(f"[cf_tempmail] 请求失败 {method} {path}: {last_error}")

    # ---------- 建地址 ----------
    def get_email(self):
        domains = [d.strip() for d in self.domains_cfg.split(",") if d.strip()]

        self.email_name = self.current_email.split("@")[0]
        self.domain = None if (self.auto_domain or not domains) else random.choice(domains)

        body = {"name": self.email_name}
        if self.domain:
            body["domain"] = self.domain
        if self.random_subdomain:
            body["enableRandomSubdomain"] = True

        log(f"创建临时邮箱 (模式: {self.mode}, 域名: {self.domain or '服务端自动'})")
        if self.mode == "admin":
            data = self._request("POST", "/admin/new_address", json=body).json()
        else:
            # 用户模式服务端强制 enablePrefix=true, 以返回的 address 为准
            data = self._request("POST", "/api/new_address", json=body).json()

        self.email = data["address"]
        self.jwt = data.get("jwt")
        log(f"临时邮箱已创建: {self.email}")
        return self.email

    # ---------- 收验证码 ----------
    def get_verify_code(self):
        last_error = "未获取到目标邮件"
        poll_start = time.time()
        max_polls = max(self.max_request_retries, 20)
        # 微软发码有延迟, 至少轮询 2 分钟
        for poll in range(max_polls):
            try:
                mails = self._fetch_mails()
                for mail in mails:
                    code = self._extract_code(mail)
                    if code:
                        log(f"验证码已提取: {code} (第 {poll + 1} 次轮询, 耗时 {time.time() - poll_start:.0f}s)", "SUCCESS")
                        return code
                if (poll + 1) % 5 == 0:
                    log(f"验证码轮询第 {poll + 1}/{max_polls} 次: 已收 {len(mails)} 封, 未见目标邮件 ({time.time() - poll_start:.0f}s)")
                last_error = "未获取到目标邮件"
            except Exception as e:
                last_error = e
                log(f"验证码轮询第 {poll + 1} 次请求异常: {e}", "WARN")
            self.page.wait_for_timeout(6000)
        else:
            log(f"验证码获取失败 ({max_polls} 次轮询, {time.time() - poll_start:.0f}s): {last_error}", "ERROR")
            raise TimeoutError

    def _fetch_mails(self):
        if self.mode == "admin":
            resp = self._request("GET", f"/admin/mails?address={quote(self.email)}&limit=20&offset=0")
        else:
            resp = self._request(
                "GET", "/api/mails?limit=20&offset=0",
                extra_headers={"Authorization": f"Bearer {self.jwt}"}
            )
        return resp.json().get("results") or []

    @staticmethod
    def _unescape_unicode(s):
        """还原正文中的 \\uXXXX 转义 (微软部分自动邮件的 text/plain 是转义文本)"""
        return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)

    def _extract_code(self, mail):
        """
        新版 cloudflare_temp_email 只存 raw MIME (无独立 subject/message 列),
        用标准库解析出主题与纯文本正文; 兼容旧版的 subject/message 字段。
        """
        subject = mail.get("subject") or ""
        content = mail.get("message") or ""
        raw = mail.get("raw") or ""

        if raw and not (subject and content):
            try:
                msg = email_lib.message_from_string(raw)
                if not subject:
                    raw_subject = msg.get("Subject", "")
                    subject = str(email_lib.header.make_header(
                        email_lib.header.decode_header(raw_subject)))
                if not content:
                    texts = []
                    parts = msg.walk() if msg.is_multipart() else [msg]
                    for part in parts:
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or "utf-8"
                                texts.append(payload.decode(charset, "ignore"))
                    content = "\n".join(texts)
            except Exception:
                # MIME 解析失败则对整体做 quoted-printable 软解码兜底
                try:
                    content = quopri.decodestring(raw.encode()).decode("utf-8", "ignore")
                except Exception:
                    content = content or raw

        subject = self._unescape_unicode(subject)
        content = self._unescape_unicode(content)

        if self.subject in subject and self.masked_email in content:
            match = re.search(self.regex, content, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

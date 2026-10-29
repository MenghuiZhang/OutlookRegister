"""注册代理连通性探测。

移植自 grok-slim-register1/app.py 的 _probe_register_proxy 系列函数:
  - 用 requests (+PySocks) 替代 curl_cffi (本机环境无 curl_cffi)
  - 连通性目标换成 Outlook 注册链路 (signup.live.com / outlook.live.com)

两步探测:
  1) 出口 IP / 区域 (mayips → ip-api → Cloudflare → ipify)
  2) 注册站连通性 (https://signup.live.com/signup, HTTP < 500 即通过)
raw 为空 = 直连探测。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Optional

import requests

from proxy_pool import (
    ProxyConfigError,
    expand_proxy_template,
    get_proxy_pool,
    parse_proxy_line,
)

# Outlook 注册流程入口页，探活目标
SIGNUP_CHECK_URLS: tuple[str, ...] = (
    "https://signup.live.com/signup",
    "https://outlook.live.com/mail/0/?prompt=create_account",
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 专用会话: trust_env=False 彻底无视系统代理/环境变量 (macOS 上 requests 默认会
# 读取系统代理设置), 保证探测流量只走被测节点本身, 结果不受本机小火箭等影响
_probe_session = requests.Session()
_probe_session.trust_env = False


def _short_proxy_err(err: str, max_len: int = 96) -> str:
    """把 requests 长错误压成一行可读短句。"""
    s = str(err or "").strip()
    if not s:
        return "未知错误"
    low = s.lower()
    if "timed out" in low or "timeout" in low:
        m = re.search(r"after\s+(\d+)\s*milliseconds", s, re.I)
        if m:
            return f"连接超时（{m.group(1)}ms）"
        return "连接超时"
    if "socks" in low:
        return "SOCKS 握手失败（协议/鉴权？）"
    if "ssl" in low or "tls" in low or "certificate" in low:
        return "TLS/SSL 握手失败"
    if "connection refused" in low or "couldn't connect" in low or "cannot connect" in low:
        return "无法连接代理（拒绝/未监听）"
    if "authentication" in low or "auth" in low and "proxy" in low or "407" in low:
        return "代理鉴权失败（账号/密码错误？）"
    if "proxy" in low and ("unreachable" in low or "error" in low):
        return "代理不可达"
    if "name or service not known" in low or "nodename nor servname" in low or "getaddrinfo" in low:
        return "DNS 解析失败"
    if "max retries exceeded" in low:
        return "连接失败（重试耗尽）"
    if "remote end closed" in low or "connection reset" in low or "recv failure" in low:
        return "代理中断连接"
    # 去掉 requests 的长尾巴
    s = re.split(r"\s+See https?://", s, maxsplit=1)[0]
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def _build_proxies(raw_spec: str) -> Optional[dict[str, str]]:
    """spec → requests 的 proxies dict；空返回 None（直连）。"""
    if not raw_spec:
        return None
    url = raw_spec
    # requests/PySocks: 远端解析 DNS 用 socks5h
    url = url.replace("socks5://", "socks5h://", 1)
    return {"http": url, "https": url}


def _get(url: str, proxies=None, timeout: float = 8.0):
    return _probe_session.get(
        url,
        proxies=proxies,
        timeout=timeout,
        allow_redirects=True,
        headers={"User-Agent": _UA},
    )


def _probe_egress_via_proxy(proxies=None, timeout: float = 8.0) -> dict:
    """
    先测出口：IP + 国家/城市。
    主源 mayips.com；失败再 ip-api / Cloudflare / ipify(+mayips 补地区)。
    """
    t0 = time.time()
    out: dict = {
        "ok": False,
        "ip": "",
        "cc": "",
        "country": "",
        "city": "",
        "region": "",
        "timezone": "",
        "postal": "",
        "at": "",
        "source": "",
        "ms": 0,
        "error": "",
    }
    # 单源别拖太久，串行失败时别堆到 30s+；总预算耗尽后跳过剩余源
    to = max(2.0, min(10.0, float(timeout or 8.0)))
    per = min(6.0, max(1.5, to / 2))
    deadline = t0 + to
    errors: list[str] = []

    def _finish(src: str, **fields) -> dict:
        out.update(fields)
        out["ok"] = True
        out["source"] = src
        out["ms"] = round((time.time() - t0) * 1000)
        return out

    def _parse_mayips(text: str) -> Optional[dict]:
        try:
            data = json.loads((text or "").strip() or "{}")
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        ip = str(data.get("ip") or "").strip()
        if not ip:
            return None
        cc = str(data.get("country") or "").strip().upper()
        return {
            "ip": ip,
            "cc": cc[:2] if cc else "",
            "country": cc,
            "city": str(data.get("city") or "").strip(),
            "region": str(data.get("state") or "").strip(),
            "postal": str(data.get("postal") or "").strip(),
            "at": str(data.get("at") or "").strip(),
        }

    def _enrich_geo_with_mayips(ip: str) -> Optional[dict]:
        """只有裸 IP 时，用 mayips ?ip= 补国家/城市（仍走同一代理）。"""
        ip = (ip or "").strip()
        if not ip:
            return None
        try:
            r = _get(f"https://mayips.com/?ip={ip}", proxies=proxies, timeout=per)
            return _parse_mayips(r.text or "")
        except Exception:
            return None

    # 1) mayips.com（主源：IP + 地区）
    try:
        r = _get("https://mayips.com/", proxies=proxies, timeout=per)
        parsed = _parse_mayips(r.text or "")
        if parsed:
            return _finish("mayips", **parsed)
        errors.append(f"mayips:http_{getattr(r, 'status_code', 0)}")
    except Exception as e:
        errors.append(f"mayips:{type(e).__name__}:{_short_proxy_err(str(e), 48)}")

    # 2) ip-api.com
    if time.time() >= deadline:
        errors.append("egress-budget")
    else:
        try:
            r = _get(
                "http://ip-api.com/json/?fields=status,message,country,countryCode,regionName,city,timezone,query",
                proxies=proxies,
                timeout=per,
            )
            data = json.loads((r.text or "").strip() or "{}")
            if str(data.get("status") or "").lower() == "success":
                return _finish(
                    "ip-api",
                    ip=str(data.get("query") or "").strip(),
                    cc=str(data.get("countryCode") or "").upper(),
                    country=str(data.get("country") or "").strip(),
                    city=str(data.get("city") or "").strip(),
                    region=str(data.get("regionName") or "").strip(),
                    timezone=str(data.get("timezone") or "").strip(),
                )
            errors.append(f"ip-api:{data.get('message') or getattr(r, 'status_code', 0)}")
        except Exception as e:
            errors.append(f"ip-api:{type(e).__name__}:{_short_proxy_err(str(e), 48)}")

    # 3) Cloudflare trace（IP + 国家码）
    if time.time() >= deadline:
        errors.append("egress-budget")
    else:
        try:
            r = _get(
                "https://www.cloudflare.com/cdn-cgi/trace",
                proxies=proxies,
                timeout=per,
            )
            kv = {}
            for line in (r.text or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
            bare_ip = str(kv.get("ip") or "").strip()
            loc = str(kv.get("loc") or "").strip().upper()
            if bare_ip:
                # 有 loc 先用；再尝试 mayips 补城市
                extra = _enrich_geo_with_mayips(bare_ip) or {}
                return _finish(
                    "cloudflare+mayips" if extra.get("city") or extra.get("cc") else "cloudflare",
                    ip=bare_ip,
                    cc=(extra.get("cc") or (loc[:2] if loc else "")),
                    country=extra.get("country") or loc,
                    city=extra.get("city") or "",
                    region=extra.get("region") or "",
                    postal=extra.get("postal") or "",
                    at=extra.get("at") or "",
                )
            errors.append(f"cf:{getattr(r, 'status_code', 0)}")
        except Exception as e:
            errors.append(f"cf:{type(e).__name__}:{_short_proxy_err(str(e), 48)}")

    # 4) ipify 裸 IP → mayips 补地区
    if time.time() >= deadline:
        errors.append("egress-budget")
    else:
        try:
            r = _get("https://api.ipify.org", proxies=proxies, timeout=min(per, 5.0))
            bare_ip = (r.text or "").strip()
            if r.status_code == 200 and bare_ip and len(bare_ip) < 64:
                extra = _enrich_geo_with_mayips(bare_ip) or {}
                if extra:
                    return _finish("ipify+mayips", **extra)
                return _finish("ipify", ip=bare_ip)
            errors.append(f"ipify:http_{r.status_code}")
        except Exception as e:
            errors.append(f"ipify:{type(e).__name__}:{_short_proxy_err(str(e), 48)}")

    out["ms"] = round((time.time() - t0) * 1000)
    out["error"] = _short_proxy_err(" | ".join(errors) if errors else "出口探测失败")
    return out


def _format_egress_label(eg: dict) -> str:
    """出口一行：1.2.3.4 · HK Kwai Chung · Kwai Tsing District"""
    if not eg or not eg.get("ok"):
        return ""
    ip = str(eg.get("ip") or "").strip()
    cc = str(eg.get("cc") or eg.get("country") or "").strip().upper()
    city = str(eg.get("city") or "").strip()
    region = str(eg.get("region") or "").strip()
    at = str(eg.get("at") or "").strip()
    tz = str(eg.get("timezone") or "").strip()
    parts = []
    if ip:
        parts.append(ip)
    # 国家 + 城市
    geo_bits = [x for x in (cc, city) if x]
    if geo_bits:
        parts.append(" ".join(geo_bits))
    # 州/区（与城市不同才显示）
    if region and region.lower() not in (city or "").lower():
        parts.append(region)
    if at and at.lower() not in ("", "isp"):
        parts.append(at)
    if tz:
        parts.append(tz)
    return " · ".join(parts)


def probe_register_proxy(raw: str, timeout: float = 6.0) -> dict:
    """
    测试注册代理，两步：
      1) 出口 IP / 区域
      2) 注册站 (signup.live.com) 连通性
    raw 空 = 直连探测。
    """
    raw = (raw or "").strip()
    t0 = time.time()
    result: dict = {
        "ok": False,
        "proxy": raw or "(direct)",
        "mode": "proxy" if raw else "direct",
        "ms": 0,
        "via": "",
        "status": 0,
        "egress_ok": False,
        "egress_ip": "",
        "egress_cc": "",
        "egress_country": "",
        "egress_city": "",
        "egress_region": "",
        "egress_timezone": "",
        "egress_label": "",
        "egress_ms": 0,
        "signup_ok": False,
        "signup_status": 0,
        "signup_ms": 0,
        "message": "",
        "error": "",
    }
    try:
        parsed = parse_proxy_line(raw) if raw else None
    except ProxyConfigError:
        result["error"] = "代理格式无法解析"
        result["message"] = (
            "格式错误。支持 host:port · http/socks5:// · "
            "user:pass@host:port · host:port:user:pass"
        )
        result["ms"] = round((time.time() - t0) * 1000)
        return result

    proxies = None
    if parsed:
        url = parsed["spec"]
        if not url:
            result["error"] = "解析结果无 server"
            result["message"] = result["error"]
            result["ms"] = round((time.time() - t0) * 1000)
            return result
        proxies = _build_proxies(url)
        # 展示时打码密码
        server_show = f"{parsed['host']}:{parsed['port']}"
        if parsed.get("username"):
            u = str(parsed.get("username") or "")
            u_show = u if len(u) <= 24 else (u[:12] + "…" + u[-8:])
            server_show = f"{parsed['scheme']}://{u_show}:***@{parsed['host']}:{parsed['port']}"
        result["proxy"] = server_show
        result["scheme"] = parsed["scheme"] or ""

    to = max(3.0, min(30.0, float(timeout or 12.0)))
    mode = "代理" if raw else "直连"

    # ── ① 先测出口 IP / 区域（主源 mayips.com）──
    eg = _probe_egress_via_proxy(proxies=proxies, timeout=min(to, 4.0))
    result["egress_ok"] = bool(eg.get("ok"))
    result["egress_ip"] = str(eg.get("ip") or "")
    result["egress_cc"] = str(eg.get("cc") or "")
    result["egress_country"] = str(eg.get("country") or "")
    result["egress_city"] = str(eg.get("city") or "")
    result["egress_region"] = str(eg.get("region") or "")
    result["egress_timezone"] = str(eg.get("timezone") or "")
    result["egress_postal"] = str(eg.get("postal") or "")
    result["egress_at"] = str(eg.get("at") or "")
    result["egress_source"] = str(eg.get("source") or "")
    result["egress_ms"] = int(eg.get("ms") or 0)
    result["egress_label"] = _format_egress_label(eg)
    if not eg.get("ok"):
        result["egress_error"] = str(eg.get("error") or "出口探测失败")

    # ── ② 再测注册站连通性 ──
    site_err = ""
    site_t0 = time.time()
    status = 0
    for check_url in SIGNUP_CHECK_URLS:
        try:
            r = _get(check_url, proxies=proxies, timeout=to)
            status = int(getattr(r, "status_code", 0) or 0)
            if status and status < 500:
                break
        except Exception as e:
            status = 0
            site_err = f"{type(e).__name__}:{e}"
            continue
    result["status"] = status
    result["signup_status"] = status
    result["signup_ms"] = round((time.time() - site_t0) * 1000)
    result["via"] = "signup.live.com"
    if status and status < 500:
        result["signup_ok"] = True
    else:
        site_err = site_err or f"http_{status}"

    result["ms"] = round((time.time() - t0) * 1000)

    # 拼展示文案：出口 + 注册站
    eg_part = ""
    if result.get("egress_ok") and result.get("egress_label"):
        eg_part = f"出口 {result['egress_label']}"
        if result.get("egress_ms"):
            eg_part += f" ({result['egress_ms']}ms)"
    elif result.get("egress_error"):
        eg_part = f"出口失败 · {_short_proxy_err(result['egress_error'])}"
    else:
        eg_part = "出口未知"

    if result.get("signup_ok") and result.get("egress_ok"):
        result["ok"] = True
        site_part = f"注册站 OK · HTTP {result.get('signup_status') or result.get('status')} · {result.get('signup_ms') or 0}ms"
        result["message"] = f"{eg_part}  →  {site_part}"
        return result

    if result.get("signup_ok") and not result.get("egress_ok"):
        result["error"] = _short_proxy_err(result.get("egress_error") or "出口探测失败")
        result["message"] = f"{eg_part}  →  注册站 OK，但出口未通过"
        return result

    short = _short_proxy_err(site_err or "注册站不通")
    result["error"] = short
    result["error_raw"] = str(site_err)[:240]
    result["via"] = "fail" if not result.get("via") else result["via"]
    if result.get("egress_ok"):
        result["message"] = f"{eg_part}  →  注册站不通 · {short} · 总 {result['ms']}ms"
    else:
        result["message"] = f"{mode}失败 · {eg_part}  →  注册站 · {short} · 总 {result['ms']}ms"
    return result


def probe_saved_proxy(proxy_id: str, *, timeout: float = 6.0) -> dict:
    """按池内保存的 spec 探测（展开 {session}/{account} 占位符）。"""
    template = get_proxy_pool().secret_spec(proxy_id)
    probe_spec = expand_proxy_template(
        template, account="health-check", session=uuid.uuid4().hex[:16]
    )
    return probe_register_proxy(probe_spec, timeout=timeout)

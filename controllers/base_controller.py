import os
import time
import json
import random
import shutil
import threading
from datetime import datetime
from faker import Faker
from abc import ABC, abstractmethod
from utils import log

class BaseBrowserController(ABC):
    """
    所有浏览器通用的接口和共享逻辑
    """

    def __init__(self):
        with open('config.json', 'r', encoding='utf-8') as f:
            data = json.load(f)

        self.config = data
        self.wait_time = data['bot_protection_wait'] * 1000
        self.max_captcha_retries = data['max_captcha_retries']
        self.enable_oauth2 = data["oauth2"]['enable_oauth2']
        self.proxy = data['proxy']
        self.email_suffix = data['email_suffix']

        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.active_resources = []  # 记录资源以便关闭
        self.results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Results')
        os.makedirs(self.results_dir, exist_ok=True)

    # ---- 代理池租约支持 (web_console / main 每个任务开始前调用) ----
    def assign_proxy(self, lease):
        """任务级代理租约：写入线程本地，只影响当前线程之后启动的浏览器。

        lease 为 None / 直连租约时回退 config.json 的 proxy 字段。
        同时重置该线程的失败码 (供代理池释放时判定 outcome)。
        """
        tl = self.thread_local
        if lease is not None and not getattr(lease, "direct", False):
            tl.task_proxy_spec = lease.spec
            tl.task_proxy_identity = lease.identity
        else:
            tl.task_proxy_spec = ""
            tl.task_proxy_identity = ""
        tl.fail_code = ""

    def _task_proxy_option(self):
        """浏览器 launch 用的 proxy 参数。

        优先取任务租约 spec (线程级, 由代理池下发)；否则回退 config proxy。
        """
        spec = getattr(self.thread_local, "task_proxy_spec", "") or ""
        if spec:
            try:
                from proxy_pool import build_proxy_option
                option, _loopback = build_proxy_option(spec)
                return option
            except Exception as e:
                log(f"任务代理解析失败 ({e})，回退 config 代理/直连", "WARN")
                return {"server": self.proxy} if self.proxy else None
        return {"server": self.proxy} if self.proxy else None

    def _close_thread_browser(self):
        """关闭当前线程的浏览器实例 (代理节点切换时重开)。"""
        p = getattr(self.thread_local, "playwright", None)
        b = getattr(self.thread_local, "browser", None)
        if b:
            try:
                b.close()
            except Exception:
                pass
        if p:
            try:
                p.stop()
            except Exception:
                pass
        self.thread_local.playwright = None
        self.thread_local.browser = None
        with self.cleanup_lock:
            self.active_resources = [
                x for x in self.active_resources if not (x[0] is p and x[1] is b)
            ]

    def get_thread_browser(self):
        """
        通用逻辑:获取不同进程的浏览器
        """
        if hasattr(self.thread_local, "browser"):
            # 任务换了代理节点: 关掉旧浏览器, 用新节点重新 launch
            want = getattr(self.thread_local, "task_proxy_identity", "") or ""
            have = getattr(self.thread_local, "browser_proxy_identity", "") or ""
            if want != have:
                self._close_thread_browser()
        if not hasattr(self.thread_local, "browser"):
            p, b = self.launch_browser()
            if not p:
                return False

            self.thread_local.playwright = p
            self.thread_local.browser = b
            self.thread_local.browser_proxy_identity = (
                getattr(self.thread_local, "task_proxy_identity", "") or ""
            )

            with self.cleanup_lock:
                self.active_resources.append((p, b))

        return self.thread_local.browser

    @abstractmethod
    def launch_browser(self):
        """
        获取浏览器实例,返回playwright_instance, browser_instance
        """
        pass

    @abstractmethod
    def handle_captcha(self, page):
        """
        验证码处理流程
        """
        pass

    @abstractmethod 
    def clean_up(self, page=None, type="all_browser"):
        """
        清理自己创建的内容
        一个是单进程结束后关闭进程，另一个是程序结束后清除所有内容
        """
        pass

    @abstractmethod
    def get_thread_page(self):
        """
        返回页面
        """

    def get_last_pos(self):
        """获取当前线程的上一次鼠标位置 (x, y)"""
        return getattr(self.thread_local, 'last_pos', None)

    def set_last_pos(self, x, y):
        """设置当前线程的鼠标位置 (x, y)"""
        self.thread_local.last_pos = (float(x), float(y))

    def reset_last_pos(self):
        """重置当前线程的坐标历史"""
        if hasattr(self.thread_local, 'last_pos'):
            del self.thread_local.last_pos

    def wait_random_ratio(self, page, min_ratio, delta=0.02):

        actual_ratio = random.uniform(min_ratio, min_ratio + delta)
        page.wait_for_timeout(actual_ratio * self.wait_time)

    def smooth_move_to(self, page, target_x, target_y, steps=None):
        """从上一次坐标滑动到目标坐标"""
        last_pos = self.get_last_pos()
        if not last_pos:
            last_pos = (random.uniform(150, 450), random.uniform(100, 350))
            try:
                page.mouse.move(last_pos[0], last_pos[1])
            except Exception:
                pass

        if steps is None:
            steps = random.randint(6, 14)

        try:
            page.mouse.move(target_x, target_y, steps=steps)
        except Exception:
            pass

        self.set_last_pos(target_x, target_y)

    def smooth_click(self, page, locator, offset_range=5, click_delay_range=(60, 160)):
        """点击方法"""
        try:
            box = locator.bounding_box()
            if not box:
                locator.click()
                return False

            tx = box['x'] + box['width'] / 2 + random.uniform(-offset_range, offset_range)
            ty = box['y'] + box['height'] / 2 + random.uniform(-offset_range, offset_range)

            self.smooth_move_to(page, tx, ty)

            pause_ms = random.randint(click_delay_range[0], click_delay_range[1])
            page.wait_for_timeout(pause_ms)

            page.mouse.click(tx, ty)
            self.set_last_pos(tx, ty)
            return True
        except Exception:
            try:
                locator.click()
            except Exception:
                pass
            return False

    def smooth_type(self, page, locator, text, click_first=True):
        """输入方法"""
        if click_first:
            self.smooth_click(page, locator)

        for char in text:
            try:
                locator.type(char, delay=random.randint(40, 110))
            except Exception:
                break


    def get_thread_browser(self):
        """
        通用逻辑:获取不同进程的浏览器
        """
        if not hasattr(self.thread_local, "browser"):
            p, b = self.launch_browser()
            if not p:
                return False

            self.thread_local.playwright = p
            self.thread_local.browser = b

            with self.cleanup_lock:
                self.active_resources.append((p, b))

        return self.thread_local.browser

    def get_mail_provider(self, email, page):
        """
        通用逻辑:获取辅助邮箱的类 (固定使用 cf_tempmail)
        """

        from .cf_tempmail_controller import CloudflareTempMailController
        return CloudflareTempMailController(email, page)

    def _page_text_snippet(self, page, limit=300):
        """失败诊断: 当前页面可见文本摘要 (单行, 便于日志阅读)"""
        try:
            text = page.inner_text("body") or ""
        except Exception:
            try:
                text = page.content()[:limit]
            except Exception:
                return "(页面文本不可读)"
        return " | ".join(text.split())[:limit]

    def _shot(self, page, name):
        """关键步骤截图 (grok 风格): 存到本次任务的 screenshots 目录, 失败不阻断"""
        shot_dir = getattr(page, "shot_dir", None)
        if not shot_dir:
            return
        try:
            page.screenshot(path=os.path.join(shot_dir, f"{int(time.time() * 1000)}-{name}.png"))
        except Exception:
            pass

    def _prune_screenshots(self, keep=100):
        """只保留最近 keep 个任务截图目录, 防止磁盘无限增长"""
        root = os.path.join(self.results_dir, 'screenshots')
        try:
            dirs = sorted(d for d in os.listdir(root)
                          if os.path.isdir(os.path.join(root, d)) and not d.startswith('.'))
        except OSError:
            return
        for name in dirs[:-keep] if len(dirs) > keep else []:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)

    def _fail_context(self, page, step, exc=None):
        """失败时打一行: 卡住的步骤 + 当前 URL + 异常类型 + 现场截图, 替代万能的失败提示"""
        url = ""
        try:
            url = page.url
        except Exception:
            pass
        detail = f" · {type(exc).__name__}: {str(exc)[:120]}" if exc else ""
        log(f"失败于步骤[{step}] · URL: {url}{detail}", "ERROR")
        log(f"页面文本: {self._page_text_snippet(page)}", "DEBUG")
        self._shot(page, f"fail-{step}")

    def outlook_register(self, page, email, password):
        """
        通用逻辑:注册邮箱
        """

        self.reset_last_pos()
        fake = Faker()

        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1960, 2005))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 28))
        step = "打开注册页"

        # 本任务的截图目录 (grok 风格: 关键步骤 + 失败现场)
        shot_dir = os.path.join(self.results_dir, 'screenshots',
                                f"{datetime.now().strftime('%H%M%S')}-{email}")
        os.makedirs(shot_dir, exist_ok=True)
        page.shot_dir = shot_dir
        self._prune_screenshots(keep=100)
        log(f"截图目录: {shot_dir}")

        try:
            page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=20000, wait_until="domcontentloaded")
            consent_btn = page.get_by_text('同意并继续')
            consent_btn.wait_for(timeout=30000)
            start_time = time.time()
            self.wait_random_ratio(page, 0.06)
            self.smooth_click(page, consent_btn)
        except Exception as e:
            self._fail_context(page, step, e)
            if step == "打开注册页":
                log("无法进入注册界面 → 多为 IP 被拒或代理不通, 建议换节点/直连", "ERROR")
            else:
                log("加载超时或页面变化, 看上方 URL/页面文本判断是 IP 风控还是选择器失效", "ERROR")
            self.thread_local.fail_code = "IP_BLOCK"
            return False

        try:
            step = "选择邮箱域名"
            if self.email_suffix == "@hotmail.com":
                self.wait_random_ratio(page, 0.06)
                domain_btn = page.get_by_text("@outlook.com")
                self.smooth_click(page, domain_btn)
                option_btn = page.locator(f'[role="option"]:text-is("@hotmail.com")')
                self.smooth_click(page, option_btn)

            step = "填写邮箱名"
            log(f"填写邮箱名 {email}{self.email_suffix}")
            email_input = page.locator('[aria-label="新建电子邮件"]')
            self.smooth_type(page, email_input, email)
            self._shot(page, "01-email-filled")

            step = "提交邮箱名"
            primary_btn = page.locator('[data-testid="primaryButton"]')
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.04)

            step = "填写密码"
            log("填写密码")
            pwd_input = page.locator('[type="password"]')
            self.smooth_type(page, pwd_input, password)
            self.wait_random_ratio(page, 0.03)
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.03)
            self._shot(page, "02-password-submitted")

            if page.get_by_text("请重试。如果仍然不起作用，请稍后再试。").count() > 0:
                log("当前IP注册频率过快 (密码页被拒) → 换 IP/节点", "ERROR")
                self.thread_local.fail_code = "RATE_LIMIT"
                return False

            step = "填写生日"
            log(f"填写生日 {year}-{month}-{day}")
            year_input = page.locator('[name="BirthYear"]')
            if year_input.count() > 0:
                self.smooth_type(page, year_input, year)

            month_btn = page.locator('[name="BirthMonth"]')
            self.smooth_click(page, month_btn)
            self.wait_random_ratio(page, 0.03)
            m_opt = page.locator(f'[role="option"]:text-is("{month}月")')
            self.smooth_click(page, m_opt)

            self.wait_random_ratio(page, 0.03)
            day_btn = page.locator('[name="BirthDay"]')
            self.smooth_click(page, day_btn)
            self.wait_random_ratio(page, 0.03)

            d_opt = page.locator(f'[role="option"]:text-is("{day}日")')
            if d_opt.count() > 0:
                try:
                    d_opt.scroll_into_view_if_needed()
                except Exception:
                    pass
            self.smooth_click(page, d_opt)

            self.smooth_click(page, primary_btn)

            step = "填写姓名"
            log(f"填写姓名 {firstname} {lastname}")
            lname_input = page.locator('#lastNameInput')
            lname_input.wait_for(state='visible', timeout=8000)
            self.smooth_type(page, lname_input, lastname)

            self.wait_random_ratio(page, 0.02)
            fname_input = page.locator('#firstNameInput')
            fname_input.wait_for(state='visible', timeout=8000)
            self.smooth_type(page, fname_input, firstname)
            self._shot(page, "03-name-filled")

            if time.time() - start_time < self.wait_time / 1000:
                page.wait_for_timeout(self.wait_time - (time.time() - start_time) * 1000)

            step = "提交姓名"
            self.smooth_click(page, primary_btn)
            page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(state='detached', timeout=22000)
            page.wait_for_timeout(400)

            if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count() or page.get_by_text("请重试。如果仍然不起作用，请稍后再试").count() > 0:
                log("当前IP注册频率过快 (实名页被拒) → 换 IP/节点", "ERROR")
                self.thread_local.fail_code = "RATE_LIMIT"
                return False

            if page.locator('iframe#enforcementFrame').count() > 0:
                log("验证码类型错误: 出现 FunCaptcha 而非按压验证码", "ERROR")
                self._shot(page, "04-funcaptcha-frame")
                return False

            step = "按压验证码"
            log("进入按压验证码环节")
            self._shot(page, "05-captcha-entry")
            captcha_result = self.handle_captcha(page)
            if not captcha_result:
                raise TimeoutError("按压验证码未通过")
            self._shot(page, "06-captcha-passed")

        except Exception as e:
            self._fail_context(page, step, e)
            if step == "按压验证码":
                log("按压次数达上限仍未通过 → 多为指纹/IP 特征被标记", "ERROR")
            else:
                log("加载超时或页面变化, 看上方 URL/页面文本判断是 IP 风控还是选择器失效", "ERROR")
            self.thread_local.fail_code = "IP_BLOCK"
            return False

        # --- 验证码通过后，绑定辅助邮箱服务 ---
        try:
            step = "初始化辅助邮箱"
            current_full_email = f"{email}{self.email_suffix}"
            mail_provider = self.get_mail_provider(current_full_email, page)
            if not mail_provider:
                log("辅助邮箱初始化失败", "ERROR")
                return False

            # 挂载到 page 对象上，方便写入 secured_tokens.txt
            recovery_email = mail_provider.get_email()
            page.recovery_email = recovery_email
            log(f"辅助邮箱已创建: {recovery_email}")

            step = "填写辅助邮箱"
            self.wait_random_ratio(page, 0.03)

            # 验证码通过后微软要经过 20~40s 跳转链 (signup → oauth → 中断页/表单),
            # 期间页面显示 "正在加载..."; 这里耐心等待并按页面变体处理:
            #   1. 输入框直接出现 → 填写
            #   2. "帮助保护你的帐户" 中断页 → 点 "添加电子邮件" 展开表单
            #   3. 需要先点主按钮 (继续/下一步)
            step = "等待辅助邮箱页面"
            wait_start = time.time()
            last_url = page.url
            while time.time() - wait_start < 90:
                try:
                    if page.url != last_url:
                        last_url = page.url
                        log(f"页面跳转: {last_url[:110]}")
                except Exception:
                    pass

                if page.locator('input[id^="floatingLabelInput"]').count() > 0 \
                        or page.locator('input[type="email"]').count() > 0:
                    break

                add_btn = page.get_by_text("添加电子邮件", exact=True)
                if add_btn.count() == 0:
                    add_btn = page.get_by_text("Add email", exact=True)
                if add_btn.count() > 0 and add_btn.first.is_visible():
                    log("检测到 '帮助保护你的帐户' 中断页, 点击 '添加电子邮件'")
                    self._shot(page, "07a-interrupt-page")
                    self.smooth_click(page, add_btn)
                    page.wait_for_timeout(2500)
                    continue

                next_btn = page.locator('[data-testid="primaryButton"]')
                if next_btn.count() > 0 and next_btn.first.is_visible():
                    self.smooth_click(page, next_btn)
                    page.wait_for_timeout(2500)
                    continue

                page.wait_for_timeout(1000)

            step = "填写辅助邮箱"
            # 辅助邮箱输入框: ID 前缀匹配 (Microsoft 会变更加 id 编号)
            recovery_input = None
            for sel in ['input[id^="floatingLabelInput"]', '#floatingLabelInput10',
                        'input[type="email"]', 'input[aria-label*="电子邮件"]']:
                loc = page.locator(sel)
                try:
                    loc.wait_for(state="visible", timeout=6000)
                    recovery_input = loc
                    log(f"辅助邮箱输入框命中: {sel} (等待 {time.time() - wait_start:.0f}s)")
                    break
                except Exception:
                    continue
            if not recovery_input:
                dump = page.evaluate(
                    "() => [...document.querySelectorAll('input')].map(e => "
                    "({id: e.id, type: e.type, name: e.name, "
                    "aria: e.getAttribute('aria-label'), ph: e.placeholder}))")
                log(f"未找到辅助邮箱输入框, 页面 input 元素: {dump}", "WARN")
                raise RuntimeError("未找到辅助邮箱输入框")
            self.smooth_type(page, recovery_input, recovery_email)
            self._shot(page, "07-recovery-email-filled")

            self.wait_random_ratio(page, 0.03)
            next_btn = page.locator('[data-testid="primaryButton"]')
            self.smooth_click(page, next_btn)

            step = "等待验证码邮件"
            verify_code = mail_provider.get_verify_code()

            # 验证码输入框: 同样前缀匹配兜底
            code_input = None
            for sel in ['input[id^="codeEntry"]', '#codeEntry-0',
                        'input[inputmode="numeric"]', 'input[aria-label*="代码"]']:
                loc = page.locator(sel)
                try:
                    loc.wait_for(state="visible", timeout=4000)
                    code_input = loc
                    log(f"验证码输入框命中: {sel}")
                    break
                except Exception:
                    continue
            if not code_input:
                dump = page.evaluate(
                    "() => [...document.querySelectorAll('input')].map(e => "
                    "({id: e.id, type: e.type, aria: e.getAttribute('aria-label')}))")
                log(f"未找到验证码输入框, 页面 input 元素: {dump}", "WARN")
                raise RuntimeError("未找到验证码输入框")
            self.smooth_click(page, code_input)

            # 分体式 OTP 输入框 (每格一位): 逐格填入; 单格输入框: 整段输入
            step = "填写验证码"
            boxes = page.locator('input[id^="codeEntry"]')
            box_count = boxes.count()
            if box_count >= len(verify_code):
                for i, ch in enumerate(verify_code):
                    boxes.nth(i).type(ch, delay=random.randint(30, 80))
                filled = page.evaluate(
                    "() => [...document.querySelectorAll('input[id^=codeEntry]')]"
                    ".filter(b => b.value).length")
                if filled < len(verify_code):
                    raise RuntimeError(f"验证码仅填入 {filled}/{len(verify_code)} 格")
            else:
                self.smooth_type(page, code_input, verify_code, click_first=False)
            self._shot(page, "08-verify-code-filled")

        except Exception as e:
            log(f"{email}{self.email_suffix} 注册成功，但辅助邮箱绑定失败 (步骤: {step}): {e}", "ERROR")
            self._fail_context(page, step, e)
            filename = os.path.join(self.results_dir, 'unbound_accounts.txt')
            with open(filename, 'a', encoding='utf-8') as f:
                f.write(f"{email}{self.email_suffix}: {password}\n")
            return False

        filename = os.path.join(self.results_dir, 'secured_accounts.txt' if self.enable_oauth2 else 'secured_no_token.txt')
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"{email}{self.email_suffix}: {password}----{recovery_email}\n")
        log(f"注册成功且辅助邮箱已绑定 - {email}{self.email_suffix}", "SUCCESS")

        if not self.enable_oauth2:
            return True

        step = "等待邮箱初始化"
        try:
            # 最多重试一次
            try:
                page.locator('[aria-label="新邮件"]').wait_for(timeout=32000)
                self._shot(page, "09-mailbox-ready")
                return True
            except:
                page.reload(timeout=10000)
                page.locator('[aria-label="新邮件"]').wait_for(timeout=20000)
                self._shot(page, "09-mailbox-ready")
                return True
        except Exception as e:
            self._fail_context(page, step, e)
            log("邮箱未初始化，无法正常收件 (Outlook 主界面未加载)", "ERROR")
            return False


class BaseEmailProvider(ABC):
    """
    邮件通用接口
    """

    def __init__(self, email, page):

        self.page = page
        self.current_email = email
        self.regex = r"安全代码: (\d{6})"
        self.subject = "个人 Microsoft 帐户安全代码"
        self.masked_email = f"{self.current_email[:2]}**{self.current_email.split('@')[0][-1]}@{self.current_email.split('@')[1]}"

        with open('config.json', 'r', encoding='utf-8') as f:
            self.config = json.load(f)

    @abstractmethod
    def get_email(self):
        """获取邮箱"""
        pass

    @abstractmethod
    def get_verify_code(self) -> str:
        """获取验证码，注意用掩码(36^3)"""
        pass


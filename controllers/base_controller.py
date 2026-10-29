import os
import time
import json
import random
import threading
from faker import Faker
from abc import ABC, abstractmethod

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
                print(f"[Proxy] 任务代理解析失败 ({e})，回退 config 代理/直连")
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

        try:
            page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=20000, wait_until="domcontentloaded")
            consent_btn = page.get_by_text('同意并继续')
            consent_btn.wait_for(timeout=30000)
            start_time = time.time()
            self.wait_random_ratio(page, 0.06)
            self.smooth_click(page, consent_btn)
        except Exception:
            print("[Error: IP] - IP质量不佳，无法进入注册界面。")
            self.thread_local.fail_code = "IP_BLOCK"
            return False

        try:
            if self.email_suffix == "@hotmail.com":
                self.wait_random_ratio(page, 0.06)
                domain_btn = page.get_by_text("@outlook.com")
                self.smooth_click(page, domain_btn)
                option_btn = page.locator(f'[role="option"]:text-is("@hotmail.com")')
                self.smooth_click(page, option_btn)


            email_input = page.locator('[aria-label="新建电子邮件"]')
            self.smooth_type(page, email_input, email)

            primary_btn = page.locator('[data-testid="primaryButton"]')
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.04)

            pwd_input = page.locator('[type="password"]')
            self.smooth_type(page, pwd_input, password)
            self.wait_random_ratio(page, 0.03)
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.03)

            if page.get_by_text("请重试。如果仍然不起作用，请稍后再试。").count() > 0:
                print("[Error: IP or browser] - 当前IP注册频率过快。检查IP与是否为指纹浏览器并关闭了无头模式。")
                self.thread_local.fail_code = "RATE_LIMIT"
                return False

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

            lname_input = page.locator('#lastNameInput')
            lname_input.wait_for(state='visible', timeout=8000)
            self.smooth_type(page, lname_input, lastname)

            self.wait_random_ratio(page, 0.02)
            fname_input = page.locator('#firstNameInput')
            fname_input.wait_for(state='visible', timeout=8000)
            self.smooth_type(page, fname_input, firstname)

            if time.time() - start_time < self.wait_time / 1000:
                page.wait_for_timeout(self.wait_time - (time.time() - start_time) * 1000)

            self.smooth_click(page, primary_btn)
            page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(state='detached', timeout=22000)
            page.wait_for_timeout(400)

            if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count() or page.get_by_text("请重试。如果仍然不起作用，请稍后再试").count() > 0:
                print("[Error: IP or browser] - 当前IP注册频率过快。检查IP与是否为指纹浏览器并关闭了无头模式。")
                self.thread_local.fail_code = "RATE_LIMIT"
                return False

            if page.locator('iframe#enforcementFrame').count() > 0:
                print("[Error: FunCaptcha] - 验证码类型错误，非按压验证码。")
                return False

            captcha_result = self.handle_captcha(page)
            if not captcha_result:
                raise TimeoutError

        except Exception:
            print("[Error: IP] - 加载超时或因触发机器人检测导致按压次数达到最大仍未通过。")
            self.thread_local.fail_code = "IP_BLOCK"
            return False

        # --- 验证码通过后，绑定辅助邮箱服务 ---
        try:
            current_full_email = f"{email}{self.email_suffix}"
            mail_provider = self.get_mail_provider(current_full_email, page)
            if not mail_provider:
                print("[Error: Config] - 辅助邮箱初始化失败")
                return False

            # 挂载到 page 对象上，方便写入 secured_tokens.txt
            recovery_email = mail_provider.get_email()
            page.recovery_email = recovery_email

            self.wait_random_ratio(page, 0.03)
            next_btn = page.locator('[data-testid="primaryButton"]')
            self.smooth_click(page, next_btn)

            # 辅助邮箱输入框: ID 前缀匹配 (Microsoft 会变更加 id 编号)
            recovery_input = None
            for sel in ['input[id^="floatingLabelInput"]', '#floatingLabelInput10',
                        'input[type="email"]', 'input[aria-label*="电子邮件"]']:
                loc = page.locator(sel)
                try:
                    loc.wait_for(state="visible", timeout=4000)
                    recovery_input = loc
                    print(f"[Debug] 辅助邮箱输入框命中: {sel}")
                    break
                except Exception:
                    continue
            if not recovery_input:
                dump = page.evaluate(
                    "() => [...document.querySelectorAll('input')].map(e => "
                    "({id: e.id, type: e.type, name: e.name, "
                    "aria: e.getAttribute('aria-label'), ph: e.placeholder}))")
                print("[Debug] 未找到辅助邮箱输入框, 页面 input 元素:", dump)
                raise RuntimeError("未找到辅助邮箱输入框")
            self.smooth_type(page, recovery_input, recovery_email)

            self.wait_random_ratio(page, 0.03)
            next_btn = page.locator('[data-testid="primaryButton"]')
            self.smooth_click(page, next_btn)

            verify_code = mail_provider.get_verify_code()

            # 验证码输入框: 同样前缀匹配兜底
            code_input = None
            for sel in ['input[id^="codeEntry"]', '#codeEntry-0',
                        'input[inputmode="numeric"]', 'input[aria-label*="代码"]']:
                loc = page.locator(sel)
                try:
                    loc.wait_for(state="visible", timeout=4000)
                    code_input = loc
                    print(f"[Debug] 验证码输入框命中: {sel}")
                    break
                except Exception:
                    continue
            if not code_input:
                dump = page.evaluate(
                    "() => [...document.querySelectorAll('input')].map(e => "
                    "({id: e.id, type: e.type, aria: e.getAttribute('aria-label')}))")
                print("[Debug] 未找到验证码输入框, 页面 input 元素:", dump)
                raise RuntimeError("未找到验证码输入框")
            self.smooth_click(page, code_input)

            # 分体式 OTP 输入框 (每格一位): 逐格填入; 单格输入框: 整段输入
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

        except Exception as e:
            print(f"[Error: Proof Email] - {email}{self.email_suffix}注册成功，但辅助邮箱绑定失败: {e}")
            filename = os.path.join(self.results_dir, 'unbound_accounts.txt')
            with open(filename, 'a', encoding='utf-8') as f:
                f.write(f"{email}{self.email_suffix}: {password}\n")
            return False

        filename = os.path.join(self.results_dir, 'secured_accounts.txt' if self.enable_oauth2 else 'secured_no_token.txt')
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"{email}{self.email_suffix}: {password}---{recovery_email}\n")
        print(f'[Success: Email Registration & Security Bound] - {email}{self.email_suffix}: {password}---{recovery_email}')

        if not self.enable_oauth2:
            return True

        try:
            # 最多重试一次
            try:
                page.locator('[aria-label="新邮件"]').wait_for(timeout=32000)
                return True
            except:
                page.reload(timeout=10000)
                page.locator('[aria-label="新邮件"]').wait_for(timeout=20000)
                return True
        except Exception:
            print('[Error: Timeout] - 邮箱未初始化，无法正常收件。')
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


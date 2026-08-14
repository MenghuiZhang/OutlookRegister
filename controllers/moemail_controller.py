import re
import time
import random
import requests
from get_token import get_proxy
from .base_controller import BaseEmailProvider


class MoemailController(BaseEmailProvider):

    def __init__(self, email, page):
        super().__init__(email, page)

        self.proxy = get_proxy()
        self.apikey = self.config["moemail"]["apikey"]
        self.base_url = self.config["moemail"]["base_url"]
        self.headers = {
            "X-API-Key": self.apikey,
            "Content-Type": "application/json"
        }

        # 请求需要在 headers 后
        self.expire_time = self.config["moemail"]["expire_time"]
        self.max_request_retries = self.config["moemail"]["max_request_retries"]

        suffix_cfg = self.config["moemail"]["suffix"]
        self.suffix = [s.strip() for s in suffix_cfg.split(',') if s.strip()] if suffix_cfg else self._try_get_domain()

        if self.expire_time not in (0, 3600000, 86400000, 604800000):
            self.expire_time = 3600000

    def get_email(self):

        self.email_name = self.current_email.split('@')[0]
        self.domain = random.choice(self.suffix)
        return self._try_get_email()

    def get_verify_code(self):

        last_error = "未获取到目标邮件"
        for _ in range(self.max_request_retries):
            try:
                response = requests.get(
                    f"{self.base_url}/api/emails/{self.email_id}",
                    headers=self.headers,
                    proxies=self.proxy
                )

                data = response.json()
                for message in data["messages"]:
                    if self.subject in message["subject"] and self.masked_email in message["content"]:
                        verification_code_match = re.search(self.regex, message["content"], re.IGNORECASE)
                        return verification_code_match.group(1)
                else:
                    self.page.wait_for_timeout(6000)
                    last_error = "未获取到目标邮件"

            except Exception as e:
                last_error = e
                self.page.wait_for_timeout(6000)
        else:
            print(f"获取验证码错误:{last_error}")
            raise TimeoutError

    def _try_get_email(self):
        last_error = None
        for _ in range(self.max_request_retries):
            try:
                response = requests.post(
                    f"{self.base_url}/api/emails/generate", 
                    headers= self.headers,
                    json={
                        "name": self.email_name,
                        "expiryTime": self.expire_time,
                        "domain": self.domain
                    },
                    proxies=self.proxy
                )

                self.email, self.email_id = response.json()["email"], response.json()["id"]
                return self.email
            except Exception as e:
                last_error = e
                self.page.wait_for_timeout(6000)
        else:
            print(f"获取邮箱错误:{last_error}")
            raise TimeoutError

    def _try_get_domain(self):
        last_error = None
        for _ in range(self.max_request_retries):
            try:
                response = requests.get(
                    f"{self.base_url}/api/config",
                    headers=self.headers,
                    proxies=self.proxy
                )
                return response.json()["emailDomains"].split(',')
            except Exception as e:
                last_error = e
                self.page.wait_for_timeout(6000)
        else:
            print(f"获取可用域名错误:{last_error}")
            raise TimeoutError

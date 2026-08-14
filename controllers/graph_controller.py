import re
import time
import json
import random
import requests
import threading
from get_token import get_proxy
from .base_controller import BaseEmailProvider
from datetime import datetime, timezone, timedelta


class OutlookGraphController(BaseEmailProvider):

    # 锁与 Token 池 (email -> {access_token, refresh_token, expire_time, lock})
    _graph_lock = threading.Lock()
    _token_cache = {}

    def __init__(self, email, page):
        super().__init__(email, page)

        self.strategy = self.config["outlookgraph"]["strategy"]
        self.client_id = self.config["outlookgraph"]["client_id"]
        self.max_request_retries = self.config["outlookgraph"]["max_request_retries"]
        self.created_at_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def get_email(self):
        # 线程安全锁：防止多线程读文件发生抽号竞争
        with OutlookGraphController._graph_lock:
            # 过滤旧验证码邮件
            self.created_at_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime('%Y-%m-%dT%H:%M:%SZ')

            if self.strategy == "single_email":
                target_email = self.config["outlookgraph"]["single_email"]["email"]
                file_refresh_token = self.config["outlookgraph"]["single_email"]["refresh_token"]
                file_access_token, file_expire_time = None, 0

            if self.strategy == "file":
                # 默认所有的均为填写的client_id
                with open(self.config["outlookgraph"]["file"]["path"], "r", encoding="utf-8") as f:
                    data = f.read()

                slice_data = random.choice([l for l in data.splitlines() if '@' in l and len(l.split('---')) >= 5]).split("---")
                target_email, file_refresh_token, file_access_token, file_expire_time = slice_data[0], slice_data[2], slice_data[3], float(slice_data[4])


            self.email = target_email

            # 检查缓存池：若该邮箱已经被其他线程刷新过，直接复用缓存池中最新 Token
            if self.email in OutlookGraphController._token_cache:
                cache = OutlookGraphController._token_cache[self.email]
                self.access_token = cache.get("access_token")
                self.refresh_token = cache.get("refresh_token")
                self.expire_time = cache.get("expire_time")
            else:
                # 缓存池不存在时，初始化并赋予其专属锁
                self.access_token = file_access_token
                self.refresh_token = file_refresh_token
                self.expire_time = file_expire_time
                OutlookGraphController._token_cache[self.email] = {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expire_time": self.expire_time,
                    "lock": threading.Lock() 
                }

        return self.email


    def get_verify_code(self):

        graph_endpoint = 'https://graph.microsoft.com/v1.0'
        query_params = {
            '$filter': f"receivedDateTime ge {self.created_at_iso} and subject eq '{self.subject}'",
            '$top': 25,
            '$select': 'subject,receivedDateTime,from,body',
            '$orderby': 'receivedDateTime DESC'
        }

        last_error = "未获取到目标邮件"
        for _ in range(self.max_request_retries):

            self.refresh_access_token()
            try:
                headers = {
                    'Authorization': f'Bearer {self.access_token}',
                    'Accept': 'application/json',
                    'Prefer': 'outlook.body-content-type="text"'
                }
                response = requests.get(
                f'{graph_endpoint}/me/messages',
                headers=headers,
                params=query_params,
                proxies=get_proxy()
            )
                messages = response.json()['value']

                for message in messages:
                    body_content = message.get('body', {}).get('content', '')
                    if self.masked_email in body_content:
                        match = re.search(self.regex, body_content, re.IGNORECASE)
                        return match.group(1)
                self.page.wait_for_timeout(6000)
                last_error = "未获取到目标邮件"
            except Exception as e:
                last_error = e
                self.page.wait_for_timeout(6000)
        else:
            print(f"获取验证码错误:{last_error}")
            raise TimeoutError    

    def refresh_access_token(self):

        with OutlookGraphController._graph_lock:
            cache = OutlookGraphController._token_cache.get(self.email, {})
            email_lock = cache.get("lock")

        # 使用特定邮箱锁，防止在全局锁内发请求
        with email_lock:

            with OutlookGraphController._graph_lock:
                latest_cache = OutlookGraphController._token_cache.get(self.email, {})
                acc_token = latest_cache.get("access_token")
                exp_time = latest_cache.get("expire_time")
                if latest_cache.get("refresh_token"):
                    self.refresh_token = latest_cache.get("refresh_token")

            # 检查排队期间前一个线程是否已经刷好 Token
            if acc_token and exp_time and time.time() < float(exp_time):
                self.access_token = acc_token
                self.expire_time = exp_time
                return

            self._try_refresh_access_token()

            # 刷新成功后，进全局锁更新缓存池并更新文件
            with OutlookGraphController._graph_lock:
                if self.email in OutlookGraphController._token_cache:
                    OutlookGraphController._token_cache[self.email].update({
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token,
                        "expire_time": self.expire_time
                    })
                self._save_token_to_storage()

    def _save_token_to_storage(self):

        if self.strategy == "single_email" and self.refresh_token != self.config["outlookgraph"]["single_email"]["refresh_token"]:
            self.config["outlookgraph"]["single_email"]["refresh_token"] = self.refresh_token
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)

        elif self.strategy == "file":
            file_path = self.config["outlookgraph"]["file"]["path"]
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()

                new_lines = []
                for line in lines:
                    if line.startswith(self.email):
                        parts = line.split("---")
                        if len(parts) >= 5:
                            parts[2] = self.refresh_token
                            parts[3] = self.access_token
                            parts[4] = str(self.expire_time)
                            line = "---".join(parts)
                    new_lines.append(line)

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(new_lines) + ("\n" if new_lines else ""))
            except Exception as e:
                print(f"[ERROR] 更新 Token 文件失败: {e}")

    def _try_refresh_access_token(self):
        token_url = 'https://login.microsoftonline.com/common/oauth2/v2.0/token'
        refresh_params = {
            'client_id': self.client_id,
            'refresh_token': self.refresh_token,
            'grant_type': 'refresh_token',
        }
        last_error = None
        for _ in range(self.max_request_retries):
            try:
                response = requests.post(token_url, data=refresh_params, proxies=get_proxy())
                tokens = response.json()

                self.access_token = tokens['access_token']
                self.expire_time = time.time() + tokens['expires_in']

                if 'refresh_token' in tokens:
                    self.refresh_token = tokens['refresh_token']
                break
            except Exception as e:
                last_error = e
                self.page.wait_for_timeout(6000)
        else:
            print(f"[ERROR] 刷新 Token 失败: {last_error}")
            raise TimeoutError

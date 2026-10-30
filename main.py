import os
import sys
import time
import json
from get_token import get_access_token
from concurrent.futures import ThreadPoolExecutor
from utils import random_email, generate_strong_password, log
from controllers.camoufox_controller import CamoufoxController

# 从代理池租节点时最长等待秒数 (全部被占用/冷却时排队)
PROXY_LEASE_WAIT_SECONDS = 120.0


class _StdoutTee:
    """stdout 双写: 终端照常输出, 同时追加到 Results/logs/run_<日期>.log"""

    def __init__(self, original):
        self._original = original
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Results", "logs")
        os.makedirs(log_dir, exist_ok=True)
        from datetime import date
        self._path = os.path.join(log_dir, f"run_{date.today().isoformat()}.log")
        self._fh = open(self._path, "a", encoding="utf-8")

    def write(self, text):
        try:
            self._fh.write(text)
            self._fh.flush()
        except Exception:
            pass
        return self._original.write(text)

    def flush(self):
        try:
            self._fh.flush()
        except Exception:
            pass
        return self._original.flush()

    def isatty(self):
        return getattr(self._original, "isatty", lambda: False)()

    def __getattr__(self, name):
        return getattr(self._original, name)




# --- 不确定有无帮助 ---
# 0. 视窗大小
# 1. CDP 检测：wait_for_timeout --> time.sleep()
# 2. 使用 launch_persistent_context 
# 3. 避免短时间访问
# 4. 模拟真人轨迹
# 时区

def process_single_flow(controller):
    page = None
    email = None

    try:
        page = controller.get_thread_page()

        email = random_email()
        password = generate_strong_password()
        log(f"任务开始 · 邮箱: {email}{controller.email_suffix} · 密码: {password}")

        # 调用 controller 特定的注册方法 
        result = controller.outlook_register(page, email, password)

        if result and not controller.enable_oauth2:
            log(f"任务成功 (未启用 OAuth2): {email}{controller.email_suffix}", "SUCCESS")
            return True
        elif not result:
            log(f"任务失败: {email}{controller.email_suffix} (失败原因见上方 ERROR 行)", "ERROR")
            return False

        token_result = get_access_token(page, email)
        if token_result[0]:
            refresh_token, access_token, expire_at = token_result
            with open(os.path.join(os.path.dirname(__file__), 'Results', 'secured_tokens.txt'), 'a', encoding='utf-8') as f2:
                f2.write(f"{email}{controller.email_suffix}----{password}----{refresh_token}----{access_token}----{expire_at}----{page.recovery_email}\n")
            log(f"任务成功 (含 Token): {email}{controller.email_suffix}", "SUCCESS")
            return True
        else:
            log(f"注册成功但 OAuth2 拿 token 失败: {email}{controller.email_suffix} (账号已存 secured_accounts.txt, 可稍后手动授权)", "ERROR")
            return False

    except Exception as e:
        log(f"任务异常: {type(e).__name__}: {e}", "ERROR")
        return False
    
    finally:

        controller.clean_up(page, "done_browser")


def run_single_flow_with_pool(controller, process_fn=None, tag="W"):
    """代理池版任务入口：先按调度策略租一个节点 (默认顺序轮询)，再执行注册流程。

    - 代理池为空 → 直连租约，行为与旧版一致 (回退 config.json 的 proxy 字段)
    - 租不到节点 (全部占用/冷却/停用) → 本任务跳过计失败
    - 任务结束释放租约: 成功 → success；IP 异常 → network (进冷却阶梯)；其余 → failure
    """
    from proxy_pool import get_proxy_pool

    process_fn = process_fn or process_single_flow
    pool = get_proxy_pool()
    lease = pool.acquire(tag, wait=True, wait_timeout=PROXY_LEASE_WAIT_SECONDS)
    if lease is None:
        print(f"[{tag}] 跳过：代理池没有可用节点（占用/冷却/停用）")
        return False

    controller.assign_proxy(lease)
    if lease.direct:
        print(f"[{tag}] 本轮直连（代理池为空）")
    else:
        print(f"[{tag}] 本轮节点: {lease.masked}")

    ok = False
    try:
        ok = bool(process_fn(controller))
    except Exception as e:
        print(e)
        ok = False
    finally:
        fail_code = str(getattr(controller.thread_local, "fail_code", "") or "")
        if ok:
            outcome = "success"
        elif fail_code in ("IP_BLOCK", "RATE_LIMIT"):
            outcome = "network"  # IP 质量问题 → 节点进冷却阶梯
        else:
            outcome = "failure"
        pool.release(lease, outcome=outcome, message=fail_code)
        controller.assign_proxy(None)
    return ok


def run_concurrent_flows(controller, concurrent_flows=10, max_tasks=100):
    task_counter = 0
    succeeded_tasks = 0
    failed_tasks = 0

    with ThreadPoolExecutor(max_workers=concurrent_flows) as executor:
        running_futures = set()

        while task_counter < max_tasks or len(running_futures) > 0:
            done_futures = {f for f in running_futures if f.done()}
            for future in done_futures:
                try:
                    if future.result():
                        succeeded_tasks += 1
                    else:
                        failed_tasks += 1
                except Exception as e:
                    failed_tasks += 1
                    print(e)
                running_futures.remove(future)

            while len(running_futures) < concurrent_flows and task_counter < max_tasks:
                new_future = executor.submit(run_single_flow_with_pool, controller, process_single_flow, f"W{task_counter + 1}")
                running_futures.add(new_future)
                task_counter += 1
                if max_tasks > 1 and task_counter % (max_tasks // 2) == 0:
                    print(f"已提交 {task_counter}/{max_tasks} 任务.")
                elif max_tasks == 1:
                    print(f"已提交 {task_counter}/{max_tasks} 任务.")

            time.sleep(0.5)

    print(f"\n[Result] - 共: {max_tasks}, 成功 {succeeded_tasks}, 失败 {failed_tasks}")
    log(f"全部任务结束: 共 {max_tasks}, 成功 {succeeded_tasks}, 失败 {failed_tasks}")


if __name__ == "__main__":

    with open('config.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    os.makedirs("Results", exist_ok=True)

    # stdout 落盘: 所有日志同时写入 Results/logs/run_<日期>.log, 便于事后复盘
    sys.stdout = _StdoutTee(sys.stdout)

    max_tasks = data["max_tasks"]
    concurrent_flows = data["concurrent_flows"]

    log(f"===== 启动: {max_tasks} 个任务, 并发 {concurrent_flows}, 后缀 {data['email_suffix']}, "
        f"OAuth2 {'开' if data['oauth2']['enable_oauth2'] else '关'} =====")

    selected_controller = CamoufoxController()


    try:
        run_concurrent_flows(selected_controller, concurrent_flows, max_tasks)
    finally:
        selected_controller.clean_up(type="all_browser")
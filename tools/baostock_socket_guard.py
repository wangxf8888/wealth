#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[Task#273] baostock socketutil.send_msg死连接自旋守护(日更链公共层).

根因与首个落地实现见 research/results/t270_ops_incident/REPORT.md 与
research/results/t233_touchboard_backfill/t233_runner.py (Hoss, Task#270):
官方库 socketutil.py send_msg 的 `while True: recv(8192)` 在对端关闭连接后
recv持续返回b''且不抛异常, 结尾分隔符永远匹配不到 → 无sleep用户态死循环
(2026-08-10 16:17 pid2532709自旋2.4h/91%CPU实证, socket inode已从
/proc/net/tcp消失=死连接)。

守护版语义与原库对齐(异常print+返回None), 三点差异:
  ①socket超时120s兜底(对端静默挂死不关闭也不回数据 → 120s抛timeout)
  ②空recv(对端关闭)立即返回None → 走库原生
    BSERR_RECVSOCK_FAIL(10002007)"网络接收错误"路径, 由调用方重试环
    logout+re-login重建socket接管(login内部SocketUtil().connect()重建连接)
  ③正常消息解析逻辑与原库逐行等价(含压缩分支)

用法(monkey-patch须在bs.login()前, 仅影响本进程):
    import baostock_socket_guard
    baostock_socket_guard.install()
install()幂等, 重复调用不重复替换。本模块与t233_runner内联版同源同款;
t233_runner自带内联patch不依赖本模块(Task#270已完成, 不动)。
"""
import zlib

import baostock.common.contants as _bs_cons
import baostock.common.context as _bs_ctx
import baostock.util.socketutil as _bs_sock

SOCKET_TIMEOUT = 120   # 秒: 对端静默挂死的recv/send兜底超时


def guarded_send_msg(msg):
    """守护版send_msg: 逻辑与t233_runner._t270_guarded_send_msg逐行同款。"""
    try:
        if not hasattr(_bs_ctx, "default_socket"):
            print("you don't login.")
            return None
        s = getattr(_bs_ctx, "default_socket")
        if s is None:
            return None
        s.settimeout(SOCKET_TIMEOUT)
        s.send(bytes(msg + "\n", encoding='utf-8'))
        receive = b""
        while True:
            chunk = s.recv(8192)
            if not chunk:               # 对端关闭: 原库在此死循环自旋
                print("[t273守护] recv空(连接被对端关闭), 中断请求防自旋")
                return None
            receive += chunk
            if receive[-13:] == b"<![CDATA[]]>\n":
                break
        head_bytes = receive[0:_bs_cons.MESSAGE_HEADER_LENGTH]
        head_str = bytes.decode(head_bytes)
        head_arr = head_str.split(_bs_cons.MESSAGE_SPLIT)
        if head_arr[1] in _bs_cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
            head_inner_length = int(head_arr[2])
            body_str = bytes.decode(zlib.decompress(
                receive[_bs_cons.MESSAGE_HEADER_LENGTH:
                        _bs_cons.MESSAGE_HEADER_LENGTH + head_inner_length]))
            return head_str + body_str
        return bytes.decode(receive)
    except Exception as ex:
        print(ex)
        print("接收数据异常，请稍后再试。")
        return None


def install():
    """monkey-patch socketutil.send_msg(模块级替换, 幂等)。返回True=已生效。"""
    if _bs_sock.send_msg is not guarded_send_msg:
        _bs_sock.send_msg = guarded_send_msg
    return _bs_sock.send_msg is guarded_send_msg


def installed():
    """当前进程守护是否生效(mock回归/自检用)。"""
    return _bs_sock.send_msg is guarded_send_msg

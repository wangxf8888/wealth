#!/usr/bin/env python
# -*- coding: utf-8 -*-

import baostock as bs
import pandas as pd

# 门神登录
class GateGod:
    def login(self):
        return bs.login()

    def logout(self):
        return bs.logout()


if __name__ == '__main__':
    print(GateGod().login().error_code)
    print(GateGod().logout().error_code)

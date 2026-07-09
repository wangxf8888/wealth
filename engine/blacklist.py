"""黑名单过滤器 - ST股 + 北交所"""


class Blacklist:
    """黑名单过滤器，排除ST股和北交所股票"""

    def is_blocked(self, code: str, code_name: str) -> bool:
        """
        判断股票是否在黑名单中

        Args:
            code: 股票代码，如 'sh.600519'
            code_name: 股票名称，如 '贵州茅台'

        Returns:
            True if blocked, False otherwise
        """
        # ST股: code_name中包含'ST'（大小写不敏感）
        if code_name and 'ST' in code_name.upper():
            return True

        # 北交所: code以'bj.'开头（当前数据库无此类股票，预留接口）
        if code and code.lower().startswith('bj.'):
            return True

        return False

import requests
import time

#sids表示股票列表
sids = ["sz300563", "sz002312", "sz002681"]
#成本列表，对应上面股价列表
costs = [27.953, 7.853, 3.800]
# 股票个数，对应上面的股价列表
nums = [5900, 21100, 44500]
# 每10秒循环查询所有股票的信息

# 定义一个函数，查询大A的信息
def query_market():
    headers = {'referer': 'http://finance.sina.com.cn'}
    resp = requests.get('http://hq.sinajs.cn/list=' + "sh000001", headers=headers, timeout=6)
    data = resp.text
    price = data.split(',')[3]
    # 解析出昨日收盘价是27.560
    yestclose = data.split(',')[2]
    # 解析出涨跌幅，是(当前股价-昨日收盘价)/昨日收盘价*100，取2位小数
    return round((float(price) - float(yestclose)) / float(yestclose) * 100, 2)


def main():
    while True:
        azdf = query_market()
        # 对股票列表中所有股票查询当前的交易信息
        for sid in sids:
            headers = {'referer': 'http://finance.sina.com.cn'}
            resp = requests.get('http://hq.sinajs.cn/list=' + sid, headers=headers, timeout=6)
            data = resp.text
            # data的格式：var hq_str_sz300563="神宇股份,28.660,27.560,28.020,28.960,27.810,28.010,28.020,14867830,421278715.100,2600,28.010,8400,28.000,12700,27.980,5000,27.970,3800,27.960,600,28.020,700,28.030,2900,28.040,1100,28.050,800,28.060,2024-09-12,11:19:48,00";
            #解析出当前股票名，是其中的中文字符
            name = data.split(',')[0].split('"')[1]
            #解析出股票的当前价格，是28.020
            price = data.split(',')[3]
            #解析出昨日收盘价是27.560
            yestclose = data.split(',')[2]
            #解析出涨跌幅，是(当前股价-昨日收盘价)/昨日收盘价*100，取2位小数
            zdf = round((float(price) - float(yestclose)) / float(yestclose) * 100, 2)
            # 计算当前的盈利情况，是(当前股价-成本)*股票个数
            profit = round((float(price) - costs[sids.index(sid)]) * nums[sids.index(sid)], 2)
            #打印一行信息：时间戳，股票名称，涨跌幅，[当前价格/成本价格]，盈利
            #如果涨跌幅>0，则涨跌幅显示红色，如果<0，则显示绿色，如果=0，则默认的颜色
            if zdf > 0:
                print(azdf, data.split(',')[30], data.split(',')[31], name, "\033[31m", zdf, "\033[0m", price, "/", costs[sids.index(sid)],  profit)
            elif zdf < 0:
                print(azdf, data.split(',')[30], data.split(',')[31], name, "\033[32m", zdf, "\033[0m", price, "/", costs[sids.index(sid)],  profit)
            else:
                print(azdf, data.split(',')[30], data.split(',')[31], name, zdf, price, costs[sids.index(sid)],  profit)

        time.sleep(10)


# python的main函数
if __name__ == '__main__':
    main()
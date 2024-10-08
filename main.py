# encoding：utf-8

import datetime
import baostock as bs
import pandas as pd
import os
import json
import time
import GateGod
from urllib import request


sids = []
longtou_sids = []
start_day = '2022-03-01'
today = datetime.date.today()
yestoday = today + datetime.timedelta(days=-1)


def last_trade_day_inquiry():
    is_found = False
    i = 0
    while True:
        if is_found:
            break

        i += 1
        lastday = today + datetime.timedelta(days=-i)
        rs = bs.query_trade_dates(start_date=lastday, end_date=lastday)

        while (rs.error_code == '0') & rs.next():
            # 获取一条记录，将记录合并在一起
            tmp = rs.get_row_data()

            if tmp[1] == '1':
                is_found = True
                break

    return lastday


def stock_list_init():
    # 获取证券信息
    lastday = last_trade_day_inquiry()
    rs = bs.query_all_stock(day=lastday)

    # 打印结果集
    while (rs.error_code == '0') & rs.next():
        stock_info = rs.get_row_data()
        stock_id = stock_info[0]
        stock_name = stock_info[2]

        if "sh.60" in stock_id or "sz.0" in stock_id or "sz.30" in stock_id or "sh.688" in stock_id:
            if "ST" not in stock_name:
                sid = {"id": stock_id, "name": stock_name}
                sids.append(sid)
                # print(sid)
    return sids


def is_highstop(info):
    if "sz.30" in info['code'] or "sh.68" in info['code']:
        rate = 0.2
    else:
        rate = 0.1
    preclose = round(float(info['preclose']), 2)
    delta = round(float(preclose) * rate, 2)
    highstop_price = preclose + delta
    if float(info['close']) >= highstop_price:
        return True
    else:
        return False


def parse_stock(sid):
    print("股票 %s %s分析中" % (sid['name'], sid['id']))
    score = 0
    infos = stock_history_inquiry(sid['id'], start_day, today)
    for info in infos:
        if is_highstop(info):
            score = 5
        else:
            if score > 0 and float(info['rate']) < 0.0:
                score -= 1
        if score > 0:
            print("data %s" % info['date'] + " rate = %s" % info['rate'] + " price %s" % info['close'] +
                  " high %s" % info['high'] + " low %s" % info['low'])
        pass
    return


def parse_dragonhead(sid):
    print("股票 %s %s分析中" % (sid['name'], sid['id']))
    score = 0
    demon_date = 0
    infos = stock_history_inquiry(sid['id'], start_day, today)
    for info in infos:
        if is_highstop(info):
            score += 1
            if score >= 5:
                demon_date = 10
                print("妖股诞生，请重点关注")
        else:
            score = 0
            if demon_date > 0:
                demon_date -= 1

        if score > 0 or demon_date > 0:
            print("%s" % info['date'] + " 涨跌幅 " + "%s" % info['rate'] + " 换手率 " + "%s" % info['turn'] +
                  " 成交额 " + "%s" % info['amount'] + " 股价 " + "%s" % info['close'] +
                  " 最低价 " + "%s" % info['low'] + " 开盘价 " + "%s" % info['open'])

    return


def parse_general(sid):
    print("股票 %s %s分析中" % (sid['name'], sid['id']))
    infos = stock_history_inquiry(sid['id'], start_day, today)
    key = 0
    for info in infos:
        if float(info['value']) < 25:
            return
        if float(info['value']) > 150:
            return
        if is_highstop(info):
            key += 1
            if key >= 7:
                print("妖股诞生，请重点关注")
                break
        else:
            key = 0

    if key == 7:
        for info in infos:
            print("%s" % info['date'] + " 涨跌幅 " + "%s" % info['rate'] + " 换手率 " + "%s" % info['turn'] +
                  " 成交额 " + "%s" % info['amount'] + " 股价 " + "%s" % info['close'] +
                  " 最高价 " + "%s" % info['high'] + " 最低价 " + "%s" % info['low'] +
                  " 开盘价 " + "%s" % info['open'] + " 市值 " + "%s" % info['value'])

    return


# 根据换手率查找机会点
# 1, 如果换手率比上一日大4倍，且涨幅在3%以内，则认为有买入机会
def find_buy_point_by_turn(sid):
    check_flag = 0
    oldinfo = None
    shouyi = 0
    zuigaoshouyi = 0
    print("股票 %s %s分析中" % (sid['name'], sid['id']))
    infos = stock_history_inquiry(sid['id'], start_day, today)
    for info in infos:
        if oldinfo is None:
            oldinfo = info
            continue

        # 如果正在验证阶段，则验证3天内是否上涨
        if check_flag:
            print("%s" % info['date'] + " 涨跌幅 " + "%s" % info['rate'] + " 换手率 " + "%s" % info['turn'] +
                  " 成交额 " + "%s" % info['amount'] + " 股价 " + "%s" % info['close'] +
                  " 最高价 " + "%s" % info['high'] + " 最低价 " + "%s" % info['low'] +
                  " 开盘价 " + "%s" % info['open'] + " 市值 " + "%s" % info['value'])
            shouyi += (float(info['close']) - float(oldinfo['close']))
            zuigaoshouyi += (float(info['high']) - float(oldinfo['close']))
            check_flag -= 1
        else:
            if float(info['turn']) > float(oldinfo['turn']) * 4 and float(info['rate']) < 3:
                print("%s" % info['date'] + " 涨跌幅 " + "%s" % info['rate'] + " 换手率 " + "%s" % info['turn'] +
                      " 成交额 " + "%s" % info['amount'] + " 股价 " + "%s" % info['close'] +
                      " 最高价 " + "%s" % info['high'] + " 最低价 " + "%s" % info['low'] +
                      " 开盘价 " + "%s" % info['open'] + " 市值 " + "%s" % info['value'])
                # 发现买入机会，以收盘价买入，接下来3天内验证是否上涨
                print("以 %s 买入" % info['close'])
                check_flag = 3

        oldinfo = info


# 根据成交额查找机会点
# 1， 如果连续3天成交额接近，都在+-5%以内，则认为有买入机会
def find_buy_point_by_amount(sid):
    check_flag = 0
    oldinfo = None
    shouyi = 0
    zuigaoshouyi = 0
    print("股票 %s %s分析中" % (sid['name'], sid['id']))
    infos = stock_history_inquiry(sid['id'], start_day, today)
    for info in infos:
        if oldinfo is None:
            oldinfo = info
            continue

        oldinfo = info


def parse():
    for sid in sids:
        # parse_stock(sid)
        # parse_dragonhead(sid)
        parse_general(sid)
        # find_buy_point_by_turn(sid)
        # find_buy_point_by_amount(sid)
        #time.sleep(1)
    return


def login():
    bs.login()


def logout():
    bs.logout()


def stock_history_inquiry(sid, start_day, end_day):
    history_info = []
    rs = bs.query_history_k_data_plus("%s" % sid,
                                      "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST",
                                      start_date="%s" % start_day, end_date="%s" % end_day,
                                      frequency="d", adjustflag="3")  # frequency="d"取日k线，adjustflag="3"默认不复权

    while (rs.error_code == '0') & rs.next():
        info = {}
        tmp = rs.get_row_data()
        #print(tmp)
        info['date'] = tmp[0]
        info['code'] = tmp[1]
        info['open'] = tmp[2]
        info['high'] = tmp[3]
        info['low'] = tmp[4]
        info['close'] = tmp[5]
        info['preclose'] = tmp[6]
        info['volume'] = tmp[7]
        info['amount'] = tmp[8]
        info['adjustflag'] = tmp[9]
        info['turn'] = tmp[10]
        info['tradestatus'] = tmp[11]
        info['pctChg'] = tmp[12]
        info['peTTM'] = tmp[13]
        info['pbMRQ'] = tmp[14]
        info['psTTM'] = tmp[15]
        info['pcfNcfTTM'] = tmp[16]
        info['isST'] = tmp[17]

        if info['tradestatus'] == '0':
            break
        info['rate'] = "%.2f" % ((float(info['close']) - float(info['preclose'])) * 100 / float(info['preclose']))
        info['value'] = "%.2f" % (float(info['amount']) / float(info['turn']) / 1000000)
        history_info.append(info)

    return history_info


if __name__ == '__main__':
    login()
    stock_list_init()
    parse()
    logout()
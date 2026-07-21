#!/bin/bash
# ============================================================
# AIWealth 实盘信号系统 - Cron定时任务配置
# ============================================================
# 架构: 策略无关框架 v2.0
# - 策略通过 realtime/config.py 配置, 增减策略无需改框架代码
# - 新增策略: 修改config.py + 放策略文件到strategies/
#
# 安装方法: crontab -e 后粘贴以下内容
# ============================================================

# ============================================================
# 完整crontab内容 (复制到 crontab -e):
# ============================================================
#
# # AIWealth 实盘信号系统 v2.0
# SHELL=/bin/bash
# PATH=/usr/local/bin:/usr/bin:/bin
#
# # [17:00] 晚间候选股生成 - 收盘后基于当日数据生成次日候选
# 0 17 * * 1-5 cd /home/AIWealth && python3 realtime/generate_candidates.py >> logs/realtime/generate_candidates.log 2>&1
#
# # [09:25] 早间开盘决策 - 集合竞价结束获取开盘价, 判断买入
# 25 9 * * 1-5 cd /home/AIWealth && python3 realtime/morning_decision.py >> logs/realtime/morning_decision.log 2>&1
#
# # [10:00] 盘中持仓检查 - Hour1结束
# 0 10 * * 1-5 cd /home/AIWealth && python3 realtime/position_tracker.py check >> logs/realtime/position_tracker.log 2>&1
#
# # [11:00] 盘中持仓检查 - Hour2结束
# 0 11 * * 1-5 cd /home/AIWealth && python3 realtime/position_tracker.py check >> logs/realtime/position_tracker.log 2>&1
#
# # [13:30] 盘中持仓检查 - Hour3中段
# 30 13 * * 1-5 cd /home/AIWealth && python3 realtime/position_tracker.py check >> logs/realtime/position_tracker.log 2>&1
#
# # [14:50] 尾盘持仓检查 - 收盘前最后检查
# 50 14 * * 1-5 cd /home/AIWealth && python3 realtime/position_tracker.py check >> logs/realtime/position_tracker.log 2>&1
#
# ============================================================

# ============================================================
# 手动运行示例:
# ============================================================
#
# 1. 生成候选股 (指定信号日):
#    cd /home/AIWealth && python3 realtime/generate_candidates.py --date 2026-07-18
#
# 2. 生成候选股 (自动使用DB最新交易日):
#    cd /home/AIWealth && python3 realtime/generate_candidates.py
#
# 3. 早间决策 (实盘，获取实时开盘价):
#    cd /home/AIWealth && python3 realtime/morning_decision.py
#
# 4. 早间决策 (测试，使用DB历史数据):
#    cd /home/AIWealth && python3 realtime/morning_decision.py --test-date 2026-07-18
#
# 5. 检查持仓 (实盘):
#    cd /home/AIWealth && python3 realtime/position_tracker.py check
#
# 6. 检查持仓 (测试指定日+hour):
#    cd /home/AIWealth && python3 realtime/position_tracker.py check --test-date 2026-07-18 --hour 2
#
# 7. 确认买入:
#    cd /home/AIWealth && python3 realtime/position_tracker.py confirm-buy
#
# 8. 确认卖出:
#    cd /home/AIWealth && python3 realtime/position_tracker.py confirm-sell sh.600000 10.50
#
# 9. 列出持仓:
#    cd /home/AIWealth && python3 realtime/position_tracker.py list
#
# ============================================================
# 日志文件说明:
# ============================================================
# /home/AIWealth/logs/realtime/generate_candidates.log  - 晚间候选股生成
# /home/AIWealth/logs/realtime/morning_decision.log     - 早间决策
# /home/AIWealth/logs/realtime/position_tracker.log     - 持仓跟踪
# /home/AIWealth/data/realtime/candidates_YYYYMMDD.json - 候选股数据
# /home/AIWealth/data/realtime/decision_YYYYMMDD.json   - 决策结果
# /home/AIWealth/data/realtime/positions.json           - 持仓记录
# /home/AIWealth/data/realtime/pending_buys.json        - 待确认买入
#
# ============================================================
# 配置文件:
# ============================================================
# /home/AIWealth/realtime/config.py  - 策略配置(增减策略改这里)
# ============================================================

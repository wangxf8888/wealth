# -*- coding: utf-8 -*-
"""Task#319 阶段5: PROJECT_STATUS.md 看板四处更新(python一步读改写, 规避与
progress_scheduler 10min cron的编辑窗口冲突)。已有备份backup/PROJECT_STATUS.md.bak_20260812_t319"""
import sys

P = '/home/AIWealth/PROJECT_STATUS.md'
with open(P, encoding='utf-8') as f:
    text = f.read()

# ---- 1. S5行: h1_touch注记 + solo新数字 ----
old_s5 = ("| S5 | **双板回调低吸 (two_board_pullback_dip_h1c)** | ✅早板过滤升级(7/30生效) | **+211.27%** | 79.87% | 47.84% | "
          "Task#68早板过滤参数落地(max_seal_hour_b2=1, 第2板H1内首触涨停): +99.23%→+211.27%训盲双升, "
          "0.00pp转正复验1179笔逐笔一致; 卖出=次日10:30定时(timed)不变 |")
new_s5 = ("| S5 | **双板回调低吸 (two_board_pullback_dip_h1c)** | ✅h1_touch升级(8/12用户批准方案C) | **+252.74%** | 78.78% | 47.82% | "
          "Task#319 h1_touch触板兑现落地(卖出日H1内触板卖板价/未触H1收盘照旧, slot当小时复购结构保留, 规格源#307/#310): "
          "solo +203.96%(至8/7)→**+252.74%**(至8/11, 1192笔含touch_board 84); 前序Task#68早板过滤(max_seal_hour_b2=1)不变; "
          "实盘侧同步由#320落地; 旧档案存档*_preh1touch_20260812 |")
assert text.count(old_s5) == 1, f"S5行匹配{text.count(old_s5)}处"
text = text.replace(old_s5, new_s5)

# ---- 2. 组合基线行: 新基线为主, 旧锚作对照; 并追加门控注记bullet ----
old_base = ("- **组合基线（★Task#204转正, 2026-08-06用户批准, 生产口径R2）**: 五策略(S2=expam35)+ice35:0.3:repair_exempt+drawdown-boost15:1.5 "
            "锁定窗口(2021-01-01~2026-07-01)引擎实测 **+171.73%/MDD 26.50%/Calmar 6.48**/2889笔"
            "【⚠2026-08-12注记: t299涨跌停口径统一修复(用户批准)后旧锚结构性不可复现, 当前代码配对新锚=**173.10/26.51/Calmar 6.53**/2888笔"
            "(Brock#304与Arlo#307双路自跑逐位互证), 性质=旧口径假信号/误杀被修正, 后续Δ一律对新锚】, "
            "分年199.39/147.67/104.97/94.48/435.95/46.56全正; 验证轮对t187 decision_table锚逐位PASS(anchor_check.json); "
            "滚动链最新窗口(至2026-08-06)=170.69/26.50; 旧对照: 无overlay 174.02(至8/6) / 净化t143基线173.24/34.38"
            "(CAGR仅让1.51pp换MDD压缩7.88pp) / Task#68旧口径+190.56%(含冰点+168.77%)存档作对照")
new_base = ("- **组合基线（★Task#319转正, 2026-08-12用户批准方案C, 生产口径=R2+晋级率门控+S5 h1_touch）**: "
            "五策略(S2=expam35, S5=h1_touch触板兑现)+ice35:0.3:repair_exempt+drawdown-boost15:1.5+晋级率过热门控p70:0.3(优先级ice>gate>boost) "
            "锁定窗口(2021-01-01~2026-07-01)引擎实测 **+182.22%/MDD 20.25%/Calmar 9.00**/2893笔, "
            "分年 231.99/125.84/127.52/122.27/378.33/57.37 全正; t311格C锚逐位复现(summary 14字段一致, S5 1157笔=expired1074+touch_board83); "
            "run_unified CLI默认口径=本基线(--promo-gate p70:0.3默认开启, off关闭); "
            "滚动链同步待办清单见 research/results/t319_promo_c_backtest/ROLLING_CHAIN_COMPAT.md "
            "【对照注记(旧基线保留): #204旧锚R2=**171.73/26.50/6.48**/2889笔(t299涨跌停口径修复后结构性不可复现); "
            "t299修复后配对锚=**173.10/26.51/6.53**/2888笔(Brock#304与Arlo#307双路逐位互证); 后续Δ一律对新基线182.22】; "
            "旧对照: 无overlay 174.02(至8/6) / 净化t143基线173.24/34.38 / Task#68旧口径+190.56%(含冰点+168.77%)存档作对照\n"
            "- **晋级率过热门控（★Task#319转正落地, 规格冻结链#244→#249→#250→#311）**: "
            "promo_rate(D-1首板D再板占比)∩tail_mean_h4(全市场hour4均幅)双双expanding P70(warmup120, 仅≤D历史无未来)触发的D+1过热日新开仓×0.3; "
            "窗口内作用131天(分年4/34/15/43/28/7); 生产模块backtest/promo_gate.py(纯SQL口径, sqlite ro); "
            "kill线(t249转正包)=滚动6月分化>0停用/转负连续3月复活; 实盘侧门控由#320落地")
assert text.count(old_base) == 1, f"基线行匹配{text.count(old_base)}处"
text = text.replace(old_base, new_base)

# ---- 3. 任务明细锚点下插入#319区块 ----
anchor = "## 📝 任务明细（时间倒序，最新在前）\n"
block = """
## ✅ Task #319 批准C落地A：回测侧双改动转正+档案看板刷新（2026-08-12午间, 工程席Tobias, 用户已批准方案C）

- **正式转正跑（生产默认CLI路径, --no-frontend）**: t311格C锚**逐位复现** CAGR 182.22/MDD 20.25/Calmar 9.00/2893笔, S5 1157笔(expired1074+touch_board83), summary 14字段与t311 run_combo.json逐位一致; 新基线组合档案=logs/backtest/unified_5slot_*
- **生产默认化三件套**(改前备份backup/*.bak_20260812_t319): ①新建backtest/promo_gate.py(t311_indicators纯SQL口径+GateScaler注入逐字移植, 验证脚本双PASS: 指标1336日×2列与t311 CSV逐位一致+门控日历131天/分年逐位=G1终格) ②strategies/two_board_pullback_dip_h1c.py加h1_touch出场覆盖(t307 S5H1Touch逐字) ③run_unified.py接线--promo-gate默认p70:0.3(API默认None向后兼容, off/none关闭)
- **S5 solo档案h1_touch口径刷新**(纯策略口径三overlay off, 2021-01-01~2026-08-11): +203.96%(至8/7)→**+252.74%/MDD 78.78%**/1192笔(touch_board 84), 旧档案*_preh1touch_20260812备份, log头口径注记
- **红线守卫**: 实盘侧(realtime/,execution_core.py,server.py)与前端零触碰(勘察证实realtime零调用strategy.should_sell, S5实盘卖出走position_tracker自有路径, 实盘侧由#320落地)
- **滚动链兼容(只列不改)**: P0×2=weekly_solo_refresh.sh第81行缺--promo-gate off(周日cron会污染solo纯策略口径)+daily_rolling_backtest.py run_one未传promo_gate(今晚22:30滚动为半新口径, 有h1_touch无gate); 详单research/results/t319_promo_c_backtest/ROLLING_CHAIN_COMPAT.md
"""
assert text.count(anchor) == 1
text = text.replace(anchor, anchor + block)

with open(P, 'w', encoding='utf-8') as f:
    f.write(text)
print("PROJECT_STATUS.md 三处replace+1插入 完成")

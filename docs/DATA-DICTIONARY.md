# 定义契约 freightrate

## 数值

| 列 | 类型 | 量化 | 可空 | 说明 |
|---|---|---|---|---|
| weight_kg | Decimal | 0.001 kg | 否 | 实重，输入单位 g/kg/t 都换算到 kg |
| length_cm / width_cm / height_cm | Decimal | 0.001 cm | 否 | 尺寸，输入单位 cm/m/mm 都换算到 cm |
| volumetric_kg | Decimal | 0.001 kg | 否 | 长×宽×高 ÷ volume_divisor |
| billable_kg | Decimal | 0.5 kg 向上 | 否 | 取实重与体积重的较大值后按台阶进位 |
| first_price / step_price / min_charge | Decimal | 0.01 元 | 否 | 运价表金额 |
| fuel_pct / insurance_pct | Decimal | 小数 | 否 | 8.5% 与 0.085 两种写法都归到小数 |
| declared_value | Decimal | 0.01 元 | 是 | 空值与哨兵都按 0 处理，表示未保价 |
| total_amount | Decimal | 0.01 元 | 否 | 由分项求和得出，不单独舍入 |

金额与用量一律 Decimal，对外 JSON 用字符串。禁止 float 累加。

## 单位

单位表只在 `app/config.py` 定义一份。重量 g=0.001 kg、t=1000 kg；尺寸 m=100 cm、mm=0.1 cm。
带单位后缀的单元格（如 3200g）先剥后缀，单位列非空时以单位列为准。

## 时间

- 存储：UTC，ISO 文本，秒精度，带 Z 后缀
- 输入：接受 Z、+hh:mm、无偏移三种写法；无偏移按 +08:00 解释
- 展示：页面显示 Asia/Shanghai，生成时间与批次时间都带时区名
- 运价版本窗口：`effective_from` 与 `effective_to` 也是 UTC，区间半开 [from, to)

## 空值

- `null` / 空串 / 哨兵 三者语义分开
- 哨兵清单：`-`、`--`、`/`、`N/A`、`NA`、`NULL`、`NONE`、`未知`、`无`、`空`
- 重量与尺寸出现哨兵即进隔离区；声明价值出现哨兵按未保价处理

## 半开区间

运价版本用 `[effective_from, effective_to)`：提货时刻等于 from 命中该版，等于 to 落到下一版。
相邻版本首尾相接时不重不漏。

## 去重键

业务键是运单号 `waybill_no`。同一运单号第二次出现时比较载荷
（承运商、服务、分区、提货时刻、重量、三边尺寸、声明价值）：
载荷相同记 DUPLICATE_WAYBILL，不同记 DUP_CONFLICT，两种都只保留第一条参与计费。

## 空态

- 0 字节文件：拒绝采信，返回 EMPTY_FILE
- 只有表头：采信成功但 rows_in 为 0，页面显示空态
- 全是空行：empty_rows 计数，rows_in 为 0

## 原因码

原因码只在 `app/reasons.py` 定义，分两级：`block` 表示该行不进计费或不可发布，`info` 表示只做标记。
每条隔离都带原始行号与原因码，可导出 CSV 复核。

结算裁决相关原因码：

| 代码 | 级别 | 含义 |
|---|---|---|
| RATE_MISSING | block | 基础运价无命中，无计费依据，挂起不发布 |
| PEAK_MISSING | info | 该分区无旺季加价规则（旺季加价可选，不阻断计费） |
| BASE_CONFLICT / PEAK_CONFLICT | block | 同 kind 两条以上规则同时命中，歧义挂起，不可对外发布 |
| RULE_SUPERSEDED | info | 规则在候选中但同类另一条被采用（含人工裁决未选） |
| RULE_OUT_OF_WINDOW | info | 规则不在运单提货时刻的生效窗口，未采用 |
| PEAK_APPLIED | info | 旺季加价已计入应收 |
| MANUAL_RESOLVED | info | 挂起票经人工在候选规则中裁决后采用 |
| RESULT_STALE | block | 运价指纹变化，原裁决失效，必须重算 |
| DECISION_PENDING | block | 裁决挂起，未对外发布 |

## 结算裁决状态机

- `final`：每 kind 至多一条采用规则，证据闭合，可生成票号并对外发布
- `pending`：存在同 kind 多命中或缺基础运价；票号为空，发票排除，只能人工裁决
- `stale`：裁决保存的 `rate_digest` 与当前运价指纹不一致；旧票置 void，重算后按新指纹出新票
- 票号：`BILL-` + sha256(`run_id|waybill_no|rate_digest`) 前 12 位，同输入重试必得同票号

## 旺季加价规则表（peak_rules）

列契约：`carrier,service,zone,rule_id,effective_from,effective_to,charge_type,charge_value,currency,rate_set_version`。

- 窗口同样为半开 `[effective_from, effective_to)`，时区口径与基础运价一致
- `charge_type=percent` 时 `8.5%`、`8.5`、`0.085` 都归一为小数 0.085，按基础+续重小计收取
- `charge_type=fixed` 时为固定金额，量化到 0.01 元
- 同承运商/服务/分区的同类规则区间相交，装载期即拒绝（RATE_OVERLAP）；与基础运价异类相交允许

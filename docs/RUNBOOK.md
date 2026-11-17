# 运行手册 freightrate

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18209/api/health
```

宿主机端口 18209 映射容器 8000。SQLite 落在命名卷 `freightrate-data` 的 `/app/data/freightrate.db`。

## 启动后自动完成的事

1. 建表
2. 装载 `sample_data/rate_cards.csv`（运价集 v3，38 行）与 `sample_data/peak_rules.csv`（5 条旺季加价）
3. 采信 `sample_data/waybills_dirty.csv`
4. 计算计费重、账单与结算裁决

页面首次打开即可看到 27 条运单、15 票计费、12 行隔离、应收 1548.79 元，裁决全部 final。

## 手工重跑

```bash
curl -s -X POST http://127.0.0.1:18209/api/waybills/import -F sample=waybills_dirty.csv
curl -s -X POST http://127.0.0.1:18209/api/weights
curl -s -X POST http://127.0.0.1:18209/api/charges
curl -s -X POST http://127.0.0.1:18209/api/settlement/run
curl -s http://127.0.0.1:18209/api/reconcile
```

同一份输入重复导入命中同一个 run_id；`/api/settlement/run` 重试得到同一批票号，行数与合计不变。

## 多规则命中处理

- 底价（base）与旺季加价（peak）落在同一时段是正常场景，两类规则各自独立命中
- 同 kind 多命中（如两条旺季加价窗口相交）时，相关运单裁决为 `pending`，不进发票；
  到「结算裁决」页在候选规则中人工裁决，或调 `POST /api/settlement/resolve`
- 非候选规则不能指定，接口返回 409 RULE_NOT_CANDIDATE

## 旺季加价文件

```bash
curl -s -X POST http://127.0.0.1:18209/api/peak-rules/audit -F sample=peak_rules_conflict.csv
```

同类规则区间相交返回重叠清单且 `/api/peak-rules/import` 返回 409；
装载成功会改变运价指纹，旧裁决自动置 stale、旧票置 void，需重新跑 weights → charges → settlement。

## 运价文件校验

```bash
curl -s -X POST http://127.0.0.1:18209/api/rates/audit -F sample=rate_cards_overlap.csv
```

返回重叠清单；`/api/rates/import` 在校验不通过时返回 409，不会替换现有运价。

## 重建数据库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 500，提示 RATE_MISSING | 采信时运价表还没装载 | 先打开运价手册页确认 38 行版本窗口 |
| /api/charges 返回 409 NO_WEIGHTS | 还没算计费重 | 先调 /api/weights |
| 导入后 rows_ok 为 0 | 全部行都被隔离 | 到运单页看原因码分布 |
| 运价装载 409 RATE_STALE | 提交的运价集版本低于现有 | 提高 CSV 里的 rate_set_version |
| 结算裁决出现 pending | 同 kind 规则多命中或基础价缺失 | 结算裁决页查看候选规则并人工裁决，未裁决前不发布 |
| 裁决全部 stale、发票为空 | 运价规则刚更新，指纹变化 | 依次重跑 /api/weights、/api/charges、/api/settlement/run |
| 旧票号状态为 void | 失效重算后旧票留痕，不允许复活 | 以新票号为准，void 票仅作审计痕迹 |

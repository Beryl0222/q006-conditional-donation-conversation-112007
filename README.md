# 有条件捐赠承诺的会话确认与交接

远岭公益站的合成消息、授权、交接单和签收凭证，全部联系人和站点均为虚构。

资料为 UTF-8 JSON。各类原始记录按编号分组，字段约定见 [资料格式](data/资料格式.md)。
资料浏览命令会原样输出记录，添加 `--list` 可只查看各类数量。

使用 Python 3.11，命令只依赖标准库。请在项目根目录运行。读取不可信资料时
请使用 `-I` 隔离模式，避免资料目录中的同名模块被导入。

## 资料浏览（原样输出，不做规则判断）

```sh
python3 inspect_records.py data/records.json --list
```

## 规则引擎

`donation` 包把协调员导出的群消息、转发邮件、物品名录和授权记录解析为结构化
承诺，并按**有效答复的发送顺序**重核决定权归属：

- `donation/parser.py`：中文简称/别名落到 `item_register` 的 sku 并按箱件换算；
  一句话拆成多个言语行为（请人确认、附条件接受、提出替代、转述、撤回等），
  允许同句对不同物品/批次分开答复；“就照上次”“备选一”“两种各一箱”等省略沿
  `reply_to`/`forwards` 链回溯到具体前文，缺失引用显式登记。
- `donation/analyzer.py`：授权越权（代收点只能确认收货地点，不能替捐出方同意
  少发/拆分）、配套能否拆分取决于原话、截止约定按发送时间判定、补录较早答复后
  重新核对决定权；交接单复核时把争议的未交割部分冻结、已签收部分转人工追认。
- `donation/views.py`：按每条消息的 `viewers` 与单据的 `shown_to` 过滤，导给
  某方的内容只带它有权看到的原话引用。

### 命令

```sh
# 现在可以叫谁来取货：有效承诺、待回问题、冻结库存、各方曾确认的说法
python3 -I coordinate.py data/records.json pickup

# 完整结构化分析（JSON）
python3 -I coordinate.py data/records.json analyze

# 导给某一方的交接视图（按权限过滤消息引用）
python3 -I coordinate.py data/records.json export 岭东
python3 -I coordinate.py data/records.json export 盐田 --json

# 缺失的上游来信及其影响范围；其余独立承诺照常受理
python3 -I coordinate.py data/records.json missing
```

## 测试

```sh
python3 -I run_tests.py
# 或在非隔离环境下
python3 -m unittest discover -s tests -v
```

# 有条件捐赠承诺的会话确认与交接

远岭公益站的合成消息、授权、交接单和签收凭证，全部联系人和站点均为虚构。

资料为 UTF-8 JSON。各类原始记录按编号分组，字段约定见 [资料格式](data/资料格式.md)。资料浏览命令会原样输出记录，添加 `--list` 可只查看各类数量。

使用 Python 3.11，命令只依赖标准库。请在项目根目录运行。

```sh
python3 inspect_records.py data/records.json --list
python3 -m unittest discover -s tests -v
```

## 核对承诺与交接

`donation_ledger` 把群消息、转发邮件、物品名录、授权记录、交接单和签收
记录放在一起核对。它只做有据可查的推断，宁可标“无法核对”也不替双方
决定。

```sh
# 现在可以叫谁来取货、还缺哪些答复、哪些未交割部分被冻结
python3 -m donation_ledger data/records.json pickup

# 全部承诺、单据、签收状态（JSON 便于下游使用）
python3 -m donation_ledger data/records.json status --json

# 只看某方收到的说法
python3 -m donation_ledger data/records.json status --party 盐田

# 导出给某一方看的交接内容：只带它有权看到的原话引用
python3 -m donation_ledger data/records.json export H-67/1 --party 盐田

# 某方在话题下能看到的原话
python3 -m donation_ledger data/records.json messages 盐田 --thread 岭东
```

代码分层：

- `donation_ledger/models.py`：读入资料，保留原始编号和发送／收到时间。
- `donation_ledger/text.py`：中文消息的轻量解析。把“护具”“工具”“备用
  护腕”等简称落到物品编号；处理“就照上次”“备选一”“两种各一箱”等
  省略与引用；按分句拆开对不同物品的答复；区分请人确认、附条件接受、
  提出替代、转述和撤回。解析只回答字面含义。
- `donation_ledger/authority.py`：按授权记录判断发言人能否代表某方作出
  本条所含的承诺。只授权确认收货地点的人，不能替捐出方同意少发一箱。
- `donation_ledger/ledger.py`：把上面两层拼成承诺、条件、交接单和取货
  清单。所有先后比较使用原发送时间 `sent`；`ingested` 只用来发现补录
  的早期答复，并据此重新核对谁在何时获得决定权。

## 主要判断规则

- 一份交接单只有在接受方作出有效答复、答复所附条件全部满足、物品和
  箱数不超出答复范围、且同一物资没有确认给别的接收方时才算有效。
- 原话写明“成套／成对”的报价，只同意其中一半时整条答复不能单独成立；
  原话写明“可单送／可分开”时，已同意的部分可以单独成立；原话没说明
  能否拆分、答复又只覆盖一部分时，标为待澄清。
- 约定“到某时无人确认便改送”的报价，期限只约束原指定的受赠方。改送
  方的答复以“指定方逾期未接”为前提；若指定方在期限前已有有效答复，
  改送条件不成立。
- 转述、引用和询问本身不构成承诺，即使措辞像“确认”。
- 交接单发出后补录的、原发送时间更早且相互冲突的消息，会冻结有争议
  的未交割部分，并把补确认请求发给有权的一方；双方此前看到的版本与
  对方明确接受过的内容保留在记录里。
- 已经实际签收的物品即使后来有争议，也只标为“需人工追认或补办”，
  不再当成可派发的余物。
- 缺少上游来信时，其它独立承诺照常核对；受影响的报价和交接单标为
  “无法核对”，并在 `unresolved` 里说明缺哪一封。
- 导出给某方看的交接内容只列 `viewers` 允许它看到的原话引用；其余引用
  只给出数量，提示由协调员另行转达。

命令输出里的 `status` 词汇：`accepted`（有效接受）、`withdrawn`（部分
或全部被撤回）、`condition_unmet`（所附条件不成立）、`late`（超过原约
定期限）、`unauthorized`（超出授权范围）、`incomplete`（只同意不可拆
分报价的一部分）、`needs_clarification`（原话歧义，不替双方决定）、
`relay`（转述或询问）、`counter`（还价）、`refused`（拒绝）、
`unverifiable`（缺上游引用，先受理不下结论）。

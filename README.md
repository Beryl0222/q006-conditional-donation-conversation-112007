# 有条件捐赠承诺的会话确认与交接

远岭公益站的合成消息、授权、交接单和签收凭证，全部联系人和站点均为虚构。

资料为 UTF-8 JSON。各类原始记录按编号分组，字段约定见 [资料格式](data/资料格式.md)。资料浏览命令会原样输出记录，添加 `--list` 可只查看各类数量。

使用 Python 3.11，命令只依赖标准库。请在项目根目录运行。

```sh
python3 inspect_records.py data/records.json --list
python3 -m unittest discover -v
```

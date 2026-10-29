# TXT输入适配

输入步骤调整为：

```text
TXT转录 → TXT转MD兼容格式（程序，不调用模型）
        → 原有MD解析 → 阅读批次 → 模型生成 → 校验与保存 → 主题/事件查询
原有MD ────────────────┘
```

支持UTF-8及带BOM的UTF-8文件。TXT需要保留现有转录格式，例如：

```text
[13:00:00.000 – 13:00:02.000] SPK_1: 明天下午讨论论文。
[13:00:02.000 – 13:00:04.000] SPK_2: 如果导师有空，就下午两点讨论。
```

带时间、说话人的正文也可直接作为Markdown文本，因此转换不添加标题、不合并句子、不改换行、不润色原话。除UTF-8 BOM在解码时移除外，正文保持一致，时间、说话人、行号、字符位置及原文摘要保持一致。文件扩展名不是事件或记忆的判断依据。

没有时间/说话人的普通TXT不会自动补造信息；格式不匹配或部分正文无法解析时明确拒绝。若上游TXT采用其他布局，应根据实际样例单独补充输入适配，后续算法不变。

## 使用

网页选择TXT后自动适配再导入；原有MD继续支持。命令行也自动适配：

```powershell
python -m event_memory inspect 录音.txt
python -m event_memory import 录音.txt --recording-id meeting-001
python -m event_memory process 录音.txt --recording-id meeting-001
```

inspect、import不请求模型；process沿用原来的模型处理流程并消耗已授权用量。导入会在来源metadata记录输入格式、文件名及输入正文摘要，不改变表结构。

如需单独保存一个MD文件：

```powershell
python -m event_memory txt-to-md 录音.txt --out 录音_转换.md
```

转换命令不打开数据库、不调用模型，不覆盖源文件或已有目标文件。省略--out时生成同目录同名.md；若它已存在会明确停止。

交付复跑入口支持：

```bat
start-run.cmd --input "D:\材料\录音.txt"
```

默认优先读取交付目录中的TRANSCRIPT.txt，没有时继续使用原TRANSCRIPT.md。每次新运行会将内部MD转录保存为运行目录的input.md，并记录input-metadata.json；断点恢复仍使用原冻结输入，不重新改写原请求。

同一录音跨TXT/MD导入时请使用同一个录音标识，避免因文件名不同而当作两份录音；相同正文和来源身份会沿用现有去重及成功版本保留机制。

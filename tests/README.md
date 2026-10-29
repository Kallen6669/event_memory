# 可复现工程测试

本目录仅交付合成样例、模拟模型/网络、SQLite临时库和测试源码，不包含真实转录、真实GT或模型凭据。

```powershell
python -m pip install -e ".[chatgpt]"
python -B -X utf8 -m unittest discover -s tests -v
node --test tests/web_operations.test.cjs
```

Python测试在临时目录运行，不修改演示数据库，不请求真实模型，不消耗套餐。Node未安装时前端逻辑测试会明确跳过，应补装Node再单独运行。真实模型探测不是自动化回归的一部分，不要把`chatgpt_plan_probe.py probe`当作免费离线测试。

评审反例主要在test_review_repairs.py、test_input_dependencies.py、test_operation_receipts.py、test_openai_model.py与test_adapters.py。前端断网、刷新和原编号恢复逻辑见web_operations.test.cjs。

测试通过说明这些工程行为通过，不等于独立人工语义验收完成。正式接入与独立评测材料见项目docs/主工程接入与独立验收.md。

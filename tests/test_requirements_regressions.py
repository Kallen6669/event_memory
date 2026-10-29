"""需求审查发现的行为反例；合成输入、内存库，不调用模型。"""

import unittest

from event_memory.errors import InvalidOutput, MemoryError
from event_memory.store import Store
from event_memory.transcript import build_batches, parse_transcript
from event_memory.validation import validate_output


def event(ref, title="合成讨论", **extra):
    return {"title": title, "summary": title, "evidence_refs": [ref], "claims": [],
            "topics": [], "relations": [], "continuation_of": None, "uncertainties": [], **extra}


def publish(store, recording, title="合成讨论", relations=None, candidates=None):
    raw = "[10:00:00 – 10:00:01] S: 合成讨论材料。\n"
    source = store.import_source("review", raw, recording)
    job = store.create_job("review", source["id"], recording, {"model": "audit-fixture"})
    store.job_state("review", job["id"], "running")
    output = validate_output({"events": [event("U000001", title, relations=relations or [])], "unclassified": []},
                             build_batches(parse_transcript(raw).segments)[0], candidates or {"events": [], "topics": []})
    store.save_batch("review", job["id"], 0, output)
    store.finish("review", job["id"])
    event_id = store.db.execute("SELECT id FROM events WHERE job_id=?", (job["id"],)).fetchone()[0]
    return source, store.event("review", event_id)


class RequirementRegressionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_clear_removes_only_affected_fixed_selections(self):
        _, a = publish(self.store, "A")
        _, b = publish(self.store, "B")
        only_a = self.store.select("review", [a["id"]], "A范围")
        only_b = self.store.select("review", [b["id"]], "B范围")
        mixed = self.store.select("review", [a["id"], b["id"]], "不可静默缩小的混合范围")
        self.store.clear_recording("review", "A")
        self.assertEqual(self.store.selection("review", only_b["id"]), only_b)
        for selected in (only_a, mixed):
            with self.assertRaises(MemoryError):
                self.store.selection("review", selected["id"])
        self.assertEqual(self.store.event("review", b["id"])["id"], b["id"])
        self.store.clear_recording("review", "不存在的录音")
        self.assertEqual(self.store.selection("review", only_b["id"]), only_b)

    def test_continuation_preserves_old_and_new_uncertainties_and_relations(self):
        raw = "[10:00:00 – 10:00:01] S: 先讨论甲。\n[10:00:02 – 10:00:03] S: 另有乙。\n[10:01:00 – 10:01:01] S: 继续讨论甲，是否采用未知。\n[10:02:00 – 10:02:01] S: 甲的日期也未确定。\n"
        source = self.store.import_source("review", raw, "会议")
        job = self.store.create_job("review", source["id"], "continue", {"model": "fixture"})
        self.store.job_state("review", job["id"], "running")
        segments = [s.payload() for s in parse_transcript(raw).segments]
        initial = {"events": [event("U000001", "甲", uncertainties=["负责人未知"]), event("U000002", "乙")], "unclassified": []}
        initial = validate_output(initial, {"primary": segments[:2], "context": []}, {"events": [], "topics": []})
        self.store.save_batch("review", job["id"], 0, initial)
        ids = {r["title"]: r["id"] for r in self.store.db.execute("SELECT id,title FROM events")}
        candidates = {"events": [{"id": ids["甲"], "can_continue": True}, {"id": ids["乙"], "can_continue": True}], "topics": []}
        for index, ref, unknown in ((1, "U000003", ["是否采用未知"]), (2, "U000004", ["是否采用未知", "日期未知"])):
            output = {"events": [event(ref, "甲的后续", continuation_of=ids["甲"],
                                       uncertainties=unknown, relations=[{"target_id": ids["乙"], "kind": "unknown"}])], "unclassified": []}
            output = validate_output(output, {"primary": [segments[index+1]], "context": []}, candidates)
            self.store.save_batch("review", job["id"], index, output)
        self.store.finish("review", job["id"])
        result = self.store.event("review", ids["甲"])
        self.assertEqual(result["quality"]["uncertainties"], ["负责人未知", "是否采用未知", "日期未知"])
        self.assertEqual(len(result["relations"]), 1)
        self.assertEqual(result["relations"][0]["target_id"], ids["乙"])
        self.assertEqual(result["relations"][0]["status"], "valid")

    def test_topic_candidates_rank_related_event_topics_beyond_first_eighty(self):
        for group in range(4):
            _, item = publish(self.store, f"普通{group}", "会务杂事")
            self.store.correct_topics("review", item["id"], 1, [f"A方法{i:03d}" for i in range(group*20, (group+1)*20)])
        _, target = publish(self.store, "目标", "论文甲研究方法")
        target = self.store.correct_topics("review", target["id"], 1, ["Z长期课题"])
        candidates = self.store.candidates("review", "论文甲研究方法", "新作业")
        self.assertEqual(len(self.store.topics("review")), 81)
        self.assertLessEqual(len(candidates["topics"]), 80)
        self.assertIn(target["topics"][0]["id"], {t["id"] for t in candidates["topics"]})
        self.assertEqual(self.store.candidates("other", "论文甲研究方法", "other")["topics"], [])

    def test_relation_status_changes_when_target_source_is_superseded_or_cleared(self):
        source_a, a = publish(self.store, "来源A")
        _, b = publish(self.store, "来源B", relations=[{"target_id": a["id"], "kind": "same_theme"}],
                       candidates={"events": [{"id": a["id"], "can_continue": False}], "topics": []})
        self.assertEqual(self.store.event("review", b["id"])["relations"][0]["status"], "valid")
        self.store.import_source("review", source_a["raw"] + "\n# 补充说明", "来源A", "revision2")
        self.assertEqual(self.store.event("review", b["id"])["relations"][0]["status"], "stale")
        self.store.clear_recording("review", "来源A")
        self.assertEqual(self.store.event("review", b["id"])["relations"][0]["status"], "unavailable")


class ClaimGroundingTests(unittest.TestCase):
    def check(self, text, summary, claims):
        batch = build_batches(parse_transcript(f"[10:00:00 – 10:00:03] S: {text}\n").segments)[0]
        return validate_output({"events": [event("U000001", summary, claims=claims)], "unclassified": []},
                               batch, {"events": [], "topics": []})

    @staticmethod
    def claim(kind, quote):
        return {"kind": kind, "quote": quote, "refs": ["U000001"]}

    def test_fabricated_quote_is_rejected_instead_of_rebuilding_and_keeping_decision(self):
        with self.assertRaises(InvalidOutput):
            self.check("如果明天下雨，就取消面试。", "已经决定取消明天的面试", [
                self.claim("condition", "如果明天下雨，就取消面试。"), self.claim("decision", "已经决定取消面试")])

    def test_condition_quote_cannot_keep_only_the_consequence(self):
        with self.assertRaises(InvalidOutput):
            self.check("如果明天下雨，就取消面试。", "讨论面试条件", [self.claim("condition", "取消面试")])

    def test_conditional_consequence_cannot_be_tagged_as_unconditional_decision(self):
        with self.assertRaises(InvalidOutput):
            self.check("如果明天下雨，就取消面试。", "讨论取消面试的条件", [
                self.claim("condition", "如果明天下雨，就取消面试。"), self.claim("decision", "取消面试")])

    def test_summary_cannot_turn_the_only_conditional_evidence_into_a_completed_action(self):
        with self.assertRaises(InvalidOutput):
            self.check("如果明天下雨，就取消面试。", "已经决定取消明天的面试", [
                self.claim("condition", "如果明天下雨，就取消面试。")])

    def test_both_sides_of_condition_and_an_independent_decision_are_accepted(self):
        text = "如果没贴膜，就撕下来；贴了就不用撕。我们已经决定今天先电话沟通。"
        result = self.check(text, "说明撕膜条件，并决定今天先电话沟通", [
            self.claim("condition", "如果没贴膜，就撕下来；贴了就不用撕。"),
            self.claim("decision", "我们已经决定今天先电话沟通。")])
        self.assertEqual(len(result["events"][0]["claims"]), 2)

    def test_complete_condition_with_uncertain_summary_is_accepted(self):
        result = self.check("如果明天下雨，就取消面试。", "提出下雨时取消面试的条件安排", [
            self.claim("condition", "如果明天下雨，就取消面试。")])
        self.assertEqual(result["events"][0]["claims"][0]["kind"], "condition")

    def test_postfix_condition_preserves_antecedent_and_consequence(self):
        text = "有参赛资格的话，可以提交作品。"
        self.check(text, "有资格时可以提交作品", [self.claim("condition", text)])
        for quote in ("的话，可以提交作品。", "可以提交作品。", "有参赛资格的话"):
            with self.subTest(quote=quote), self.assertRaises(InvalidOutput):
                self.check(text, "讨论提交条件", [self.claim("condition", quote)])

    def test_implicit_condition_accepts_whole_clause_but_not_only_consequence(self):
        text = "你考到五百分，那就有机会进入下一轮。"
        self.check(text, "谈达到分数后的机会", [self.claim("condition", text)])
        with self.assertRaises(InvalidOutput):
            self.check(text, "谈进入下一轮", [self.claim("condition", "那就有机会进入下一轮。")])

    def test_postfix_condition_cannot_become_completed_action(self):
        text = "没招到合适的人的话，就续费。"
        with self.assertRaises(InvalidOutput):
            self.check(text, "已经续费", [self.claim("condition", text)])

    def test_implicit_pair_can_follow_an_independent_preface(self):
        text = "我们正在申请编号，因为获得编号之后，才可以公开发放。"
        self.check(text, "说明取得编号后的发放条件", [self.claim("condition", "因为获得编号之后，才可以公开发放。")])
        with self.assertRaises(InvalidOutput):
            self.check(text, "讨论发放条件", [self.claim("condition", "才可以公开发放。")])

    def test_condition_error_identifies_the_problem_quote(self):
        with self.assertRaisesRegex(InvalidOutput, "U000001.*取消面试"):
            self.check("如果下雨，就取消面试。", "讨论条件", [self.claim("condition", "取消面试")])

    def test_evidential_negation_does_not_assert_completion(self):
        text = "如果材料齐了，就提交报名。"
        for summary in ("没有证据表明报名已经完成。", "不代表材料已经提交。", "无法确认报名已经完成。",
                        "未获独立核验，也不表示家庭已经报名或完成准备。", "这不等于报名已经完成。"):
            with self.subTest(summary=summary):
                self.check(text, summary, [self.claim("condition", text)])

    def test_evidential_negation_does_not_cover_a_separate_positive_assertion(self):
        text = "如果材料齐了，就提交报名。"
        with self.assertRaises(InvalidOutput):
            self.check(text, "没有证据表明报名已经完成，但是已经支付费用。", [self.claim("condition", text)])

    def test_independent_decision_before_an_unrelated_condition_is_not_rejected(self):
        text = "我们已经决定今天提交材料。如果明天下雨，就取消面试。"
        result = self.check(text, "决定今天提交材料，并说明面试取消的天气条件", [
            self.claim("decision", "我们已经决定今天提交材料。"),
            self.claim("condition", "如果明天下雨，就取消面试。")])
        self.assertEqual(result["events"][0]["claims"][0]["kind"], "decision")

    def test_negation_cannot_be_cut_off_to_create_an_affirmative_claim(self):
        for quote in ("决定采用net2", "我们没有决定采用net2。"):
            with self.subTest(quote=quote), self.assertRaises(InvalidOutput):
                self.check("我们没有决定采用net2。", "说话人决定采用net2", [self.claim("decision", quote)])

    def test_cancellation_can_coexist_with_a_different_negative_instruction(self):
        text = "先别发送邮件，刚才的发送安排取消了。"
        result = self.check(text, "说话人取消原发送安排", [self.claim("cancellation", text)])
        self.assertEqual(result["events"][0]["claims"][0]["kind"], "cancellation")

    def test_cautious_negative_summary_is_not_treated_as_confirmation(self):
        result = self.check("如果明天下雨，就取消面试。", "没有确认取消面试", [
            self.claim("condition", "如果明天下雨，就取消面试。")])
        self.assertEqual(result["events"][0]["summary"], "没有确认取消面试")


if __name__ == "__main__":
    unittest.main()

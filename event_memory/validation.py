"""模型输出均不可信。引用合法性不等于语义正确，后者另行评分。"""

import json
import re

from .errors import InvalidOutput

CLAIM_KINDS = {"proposal", "decision", "denial", "condition", "cancellation",
               "claimed_completion", "statement"}
CONDITIONAL = ("如果", "要是", "除非", "前提", "倘若", "假如")
CONDITION_PATTERN = re.compile("|".join(CONDITIONAL) + r"|的话|只要|只有")
CLAUSE_END = re.compile(r"[。！？!?；;]")
DEFINITE_ACTION = re.compile(r"(?:已经|已|决定|确定|确认)[^。！？!?，,；;]{0,10}?(取消|完成|改用|采用|改为|付款|支付|送达|续费|提交|发送)")
QUALIFIED = re.compile(r"如果|要是|除非|前提|倘若|假如|的话|只要|只有|条件|是否|考虑|可能|不确定|尚未|还未|未决定|没有决定|未确认|不能确认|计划|提议")
NEGATION_PREFIX = r"(?:没有|尚未|还没|并未|并非|未必|未|没|不要|不能|无法|不|别)(?:再|曾经|真正|实际|完全|已经|决定|确认|能|会|\s){0,3}"
NON_ASSERTIVE = re.compile(r"(?:不(?:代表|表示|意味着|等于|说明|证明|表明)|不是说|不能(?:确认|证明|认定|说明|表明)|无法(?:确认|证明|认定)|没有(?:证据|依据)|尚无(?:证据|依据)|未(?:确认|证实))[^。！？!?；;]{0,60}$")


def quote_locations(source: str, quote: str):
    start = source.find(quote)
    while start >= 0:
        yield start, start + len(quote)
        start = source.find(quote, start + 1)


def check_claim_condition(kind: str, quote: str, source: str, quote_span=None):
    """保护可明确定位的条件限定，不声称用词面规则证明全部语义。"""
    quote = quote.replace("\n", "")
    positions = list(quote_locations(source, quote))
    if quote_span is not None:
        positions = [(start,end) for start,end in positions if start >= quote_span[0] and end <= quote_span[1]]
    if kind == "condition":
        # 条件也可用“……的话”“考到这个分数，就……”表达。
        # 检查引文有没有截掉所在条件分句，而非强制模型使用固定六个词。
        for start, end in positions:
            complete = True
            matched_clause = False
            for marker in CONDITION_PATTERN.finditer(source):
                previous = list(CLAUSE_END.finditer(source, 0, marker.start()))
                clause_start = previous[-1].end() if previous else 0
                boundary = CLAUSE_END.search(source, marker.end())
                clause_end = boundary.start() if boundary else len(source)
                if end <= clause_start or start >= clause_end:
                    continue
                matched_clause = True
                condition_start = clause_start if marker.group() == "的话" else marker.start()
                tail = source[condition_start:clause_end].strip("，, \n")
                if tail not in quote:
                    complete = False
                    break
            if not matched_clause:
                # 没有显式标记时允许完整分句，仍拒绝只截取逗号后的后果。
                previous = list(CLAUSE_END.finditer(source, 0, start))
                clause_start = previous[-1].end() if previous else 0
                content_end = end
                while content_end > start and source[content_end-1] in " 。！？!?；;\n":
                    content_end -= 1
                boundary = CLAUSE_END.search(source, content_end)
                clause_end = boundary.start() if boundary else len(source)
                antecedent = re.split(r"[，,]", quote, maxsplit=1)[0].strip()
                implicit_pair = (len(antecedent) >= 4
                                 and not re.match(r"^(?:所以|因此|于是|那就|那么|就|才|可以|能够)", antecedent)
                                 and re.search(r"[，,][^，,。！？!?；;]{0,16}(?:才|就|方可|能够|可以)", quote))
                complete = ((not source[clause_start:start].strip("，, \n") or implicit_pair)
                            and not source[content_end:clause_end].strip("，, \n"))
            if complete:
                return
        raise InvalidOutput("条件引文截断了条件对应的表达，请引用完整条件分句。")
    if kind not in {"decision", "cancellation", "claimed_completion"}:
        return
    verbs = {
        "decision": r"(?:决定|确定|确认|采用|改用|改为)",
        "cancellation": r"(?:取消|撤回|作废)",
        "claimed_completion": r"(?:完成|办好|发出|发送|付款|支付|收到|提交|续费|点好)",
    }
    if re.search(NEGATION_PREFIX + verbs[kind], quote):
        raise InvalidOutput("逐字引文明确否定了该行动，不能保留肯定的主张类型。")
    negated = False
    for start, end in positions:
        boundaries = list(CLAUSE_END.finditer(source, 0, start))
        clause_start = boundaries[-1].end() if boundaries else 0
        content_end = end
        while content_end > start and source[content_end-1] in " 。！？!?；;\n":
            content_end -= 1
        boundary = CLAUSE_END.search(source, content_end)
        clause = source[clause_start:boundary.start() if boundary else len(source)]
        prefix = re.split(r"[，,]", source[clause_start:start])[-1]
        if re.search(NEGATION_PREFIX + r"$", prefix):
            negated = True
            continue
        if not CONDITION_PATTERN.search(clause) or CONDITION_PATTERN.search(quote):
            return
    if negated:
        raise InvalidOutput("主张引文截掉了原文的否定限定，请保留否定并修正类型。")
    raise InvalidOutput("不能从条件分句中摘出后果，标成无条件决定、取消或声称完成。")


def check_conditional_summary(item: dict, source: str):
    if not CONDITION_PATTERN.search(source):
        return
    independent = [clause for clause in CLAUSE_END.split(source) if not CONDITION_PATTERN.search(clause)]
    for description in (item["title"], item["summary"]):
        for clause in CLAUSE_END.split(description):
            if QUALIFIED.search(clause):
                continue
            for assertion in DEFINITE_ACTION.finditer(clause):
                if re.search(NEGATION_PREFIX + r"$", clause[:assertion.start()]):
                    continue
                prefix = re.split(r"但是|不过|然而|而是|但", clause[:assertion.start()])[-1]
                if NON_ASSERTIVE.search(prefix):
                    continue
                action = assertion.group(1)
                supported = any(
                    any(m.group(1) == action for m in DEFINITE_ACTION.finditer(evidence))
                    and not re.search(r"(?:不|没有|没|未|尚未).{0,3}(?:决定|确定|确认|已经|已)", evidence)
                    for evidence in independent)
                if not supported:
                    raise InvalidOutput(f"概括“{clause[:180]}”中的“{assertion.group()}”把条件表达提升为确定行动，但没有独立原文支持；请保留条件或未知。")


def text_field(value, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidOutput(f"{name} 必须是非空文本，且不超过 {maximum} 字符。")
    return value


def surrounding_claim_context(allowed, refs):
    """按相邻来源编号回读同一说话人的未结束语句；上下文不能替代实际引用位置。"""
    by_number = {int(ref[1:]): row for ref,row in allowed.items()}
    first, last = int(refs[0][1:]), int(refs[-1][1:])
    left, right = first, last
    def ended(text):
        return bool(re.search(r'[。！？!?；;][\s”’\"\')」』]*$', text))
    while left-1 in by_number:
        previous, current = by_number[left-1], by_number[left]
        if previous.get('speakers') != current.get('speakers') or ended(previous['text']):
            break
        left -= 1
    while right+1 in by_number:
        current, following = by_number[right], by_number[right+1]
        if current.get('speakers') != following.get('speakers') or ended(current['text']):
            break
        right += 1
    prefix = ''.join(by_number[i]['text'] for i in range(left,first))
    selected = ''.join(allowed[ref]['text'] for ref in refs)
    suffix = ''.join(by_number[i]['text'] for i in range(last+1,right+1))
    return prefix+selected+suffix, (len(prefix),len(prefix)+len(selected))


def validate_output(value, batch: dict, candidates: dict) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InvalidOutput("返回结果不是完整 JSON。") from exc
    if not isinstance(value, dict) or not isinstance(value.get("events"), list) or not isinstance(value.get("unclassified"), list):
        raise InvalidOutput("返回结果必须包含 events 和 unclassified 数组。")
    if len(value["events"]) > 100:
        raise InvalidOutput("单批事件数量超过限制。")
    primary = {row["ref"]: row for row in batch["primary"]}
    if primary and not value["events"] and not value["unclassified"]:
        raise InvalidOutput("非空输入不能用完全空的解释结果伪装成功。")
    allowed = {row["ref"]: row for row in batch["context"] + batch["primary"]}
    offered = {row["id"]: row for row in candidates["events"]}
    topics = {row["id"] for row in candidates["topics"]}
    covered = set()
    clean_events, warnings = [], []
    for item in value["events"]:
        if not isinstance(item, dict):
            raise InvalidOutput("事件必须是对象。")
        text_field(item.get("title"), "事件标题", 160)
        text_field(item.get("summary"), "经历概括", 4000)
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref in allowed for ref in refs):
            raise InvalidOutput("事件引用缺失、非法或超出本批允许范围。")
        if len(refs) != len(set(refs)) or not set(refs).intersection(primary):
            raise InvalidOutput("引用不得重复，事件必须依据本批主片段。")
        covered.update(set(refs).intersection(primary))
        continuation = item.get("continuation_of")
        if continuation is not None and (not isinstance(continuation, str) or continuation not in offered or not offered[continuation]["can_continue"]):
            raise InvalidOutput("延续目标必须是提供候选中的同作业未发布经历。")
        claims = item.get("claims")
        if not isinstance(claims, list) or len(claims) > 100:
            raise InvalidOutput("有限主张须为最多 100 项的数组。")
        condition_refs = set()
        clean_claims = []
        for claim in claims:
            if not isinstance(claim, dict) or not isinstance(claim.get("kind"), str) or claim["kind"] not in CLAIM_KINDS:
                raise InvalidOutput("主张类型不受支持。")
            quote = text_field(claim.get("quote"), "逐字引用", 8000)
            claim_refs = claim.get("refs")
            if not isinstance(claim_refs, list) or not claim_refs or not all(isinstance(ref, str) and ref in allowed for ref in claim_refs):
                raise InvalidOutput("主张引用必须包含在本事件依据中。")
            claim_refs = sorted(set(claim_refs))
            positions = [int(ref[1:]) for ref in claim_refs]
            if positions != list(range(positions[0], positions[-1] + 1)):
                raise InvalidOutput("单条主张须引用连续来源片段，不能跳过中间否定、条件或其他原话；请拆分主张。")
            extra_refs = [ref for ref in claim_refs if ref not in refs]
            if extra_refs:
                refs = sorted(set(refs + extra_refs))
                covered.update(set(extra_refs).intersection(primary))
                warnings.append({"ref": extra_refs[0], "action": "claim_refs_included",
                                 "reason": "把主张明确引用的合法片段补入本事件依据；不推断额外关系"})
            joined = "\n".join(allowed[ref]["text"] for ref in claim_refs)
            joined_plain = "".join(allowed[ref]["text"] for ref in claim_refs)
            quote_origin = "model_verbatim"
            if quote not in joined and quote not in joined_plain:
                raise InvalidOutput("主张引文与原文不匹配，不能替换引文后保留原主张标签；请逐字修正。")
            try:
                context, quote_span = surrounding_claim_context(allowed, claim_refs)
                check_claim_condition(claim["kind"], quote, context, quote_span)
            except InvalidOutput as exc:
                raise InvalidOutput(f"主张 {claim['kind']}，引用 {','.join(claim_refs)}，引文“{quote[:180]}”：{exc}") from exc
            if claim["kind"] == "condition":
                condition_refs.update(claim_refs)
            clean_claims.append({"kind": claim["kind"], "quote": quote,
                                 "refs": list(dict.fromkeys(claim_refs)),
                                 "quote_origin": quote_origin,
                                 "speaker_candidates": sorted({speaker for ref in claim_refs for speaker in allowed[ref]["speakers"]}),
                                 "reality_verified": False})
        # 这是针对明确条件词的保守保护，不能证明所有隐含条件都被理解。
        for ref in refs:
            if any(word in allowed[ref]["text"] for word in CONDITIONAL) and ref not in condition_refs:
                raise InvalidOutput("引用含明确条件，但没有保留对应条件主张。")
        check_conditional_summary(item, "".join(allowed[ref]["text"] for ref in sorted(refs)))
        suggestions = item.get("topics")
        if not isinstance(suggestions, list) or len(suggestions) > 12:
            raise InvalidOutput("主题建议须为最多 12 项的数组。")
        clean_topics = []
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                raise InvalidOutput("主题建议必须是对象。")
            topic_id = suggestion.get("existing_id")
            name = text_field(suggestion.get("name"), "主题名称", 120)
            if topic_id is not None and not isinstance(topic_id, str):
                raise InvalidOutput("关联了未提供或不属于当前范围的主题。")
            if topic_id is not None and topic_id not in topics:
                warnings.append({"ref": refs[0], "action": "unresolved_topic_suggestion",
                                 "reason": "模型给出的既有主题编号不在候选范围，未建立该关联；待核对主题建议：" + name})
                continue
            clean_topics.append({"existing_id": topic_id, "name": name})
        relations = item.get("relations", [])
        if not isinstance(relations, list) or len(relations) > 12:
            raise InvalidOutput("关系候选须为最多 12 项的数组。")
        for relation in relations:
            if not isinstance(relation, dict) or not isinstance(relation.get("target_id"), str) or relation["target_id"] not in offered or not isinstance(relation.get("kind"), str) or relation["kind"] not in {"same_theme", "possible_continuation", "unknown"}:
                raise InvalidOutput("关系目标或类型非法。")
        uncertainties = item.get("uncertainties", [])
        if not isinstance(uncertainties, list) or not all(isinstance(v, str) and len(v) <= 500 for v in uncertainties):
            raise InvalidOutput("未知信息应为简短文本数组。")
        clean_events.append({"title": item["title"], "summary": item["summary"],
                             "evidence_refs": refs, "claims": clean_claims, "topics": clean_topics,
                             "continuation_of": continuation, "relations": relations,
                             "uncertainties": uncertainties})
    unclassified, unclassified_set = [], set()
    for item in value["unclassified"]:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            raise InvalidOutput("未归类引用必须属于本批主片段。")
        reason = text_field(item.get("reason"), "未归类原因", 300)
        if item["ref"] not in allowed:
            warnings.append({"ref": item["ref"], "action": "discard_unoffered_unclassified",
                             "reason": "模型额外输出了范围外的未归类编号；不读取、不写入其归属，不作为证据"})
            continue
        if item["ref"] not in primary:
            warnings.append({"ref": item["ref"], "action": "context_only",
                             "reason": "上下文只供阅读，不重复改变前批归属：" + reason})
            continue
        if item["ref"] in covered:
            # 不改写证据或编造含义：保留模型已引用的原文，并显式留待人工复核。
            warnings.append({"ref": item["ref"], "action": "keep_event_evidence",
                             "reason": "模型同时归类和未归类；保留依据归属，未归类理由：" + reason})
            continue
        if item["ref"] in unclassified_set:
            raise InvalidOutput("未归类列表存在重复引用。")
        unclassified.append({"ref": item["ref"], "reason": reason})
        unclassified_set.add(item["ref"])
    for ref in sorted(set(primary) - covered - unclassified_set):
        # 模型漏解释不能伪装成已理解；明确列为系统保留的未归类，原文仍能回读。
        unclassified.append({"ref": ref, "reason": "模型未提供该片段的解释或归属；系统保留为未归类，需人工核对"})
        warnings.append({"ref": ref, "action": "unexplained_input",
                         "reason": "不补猜模型含义，保留未归类及原文"})
    return {"events": clean_events, "unclassified": unclassified, "normalization_warnings": warnings}

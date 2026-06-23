"""文档智能处理模块 — 表单/票据/合同/表格/版面/问答的纯文本智能解析。

提供以下能力（纯 Python + 正则实现，无外部模型依赖）：
  - 表单识别（FormRecognizer）：表单字段提取、键值对识别、表单模板匹配
  - 票据识别（ReceiptRecognizer）：发票/收据信息提取（金额/日期/商品/税额），结构化输出
  - 合同审查（ContractReviewer）：合同条款提取、风险点检测、关键条款比对、合规性检查
  - 文档问答（DocVQA）：基于文档内容的问题回答，支持定位原文
  - 表格识别（TableRecognizer）：表格结构识别、单元格提取、行列关系、合并单元格处理
  - 版面理解（LayoutAnalyzer）：文档版面分析，区域分类（标题/正文/图片/表格/页眉页脚）
  - DocumentIntelligencePlugin：整合以上能力的插件类
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# =====================================================================
# 数据结构定义
# =====================================================================

@dataclass
class FormField:
    """表单字段（键值对）。"""
    key: str               # 字段名
    value: str             # 字段值
    confidence: float = 1.0  # 置信度 0~1
    line: int = 0          # 所在行号


@dataclass
class Form:
    """表单识别结果。"""
    fields: List[FormField] = field(default_factory=list)
    template: str = ""         # 匹配到的模板名
    template_confidence: float = 0.0  # 模板匹配置信度


@dataclass
class ReceiptItem:
    """票据商品明细行。"""
    name: str = ""          # 商品/服务名称
    quantity: float = 1.0   # 数量
    unit_price: float = 0.0  # 单价
    amount: float = 0.0     # 金额


@dataclass
class Receipt:
    """票据结构化结果。"""
    merchant: str = ""       # 开票方/商户
    receipt_no: str = ""     # 票据号码
    date: str = ""           # 开票日期
    items: List[ReceiptItem] = field(default_factory=list)
    subtotal: float = 0.0    # 金额合计（不含税）
    tax: float = 0.0         # 税额
    total: float = 0.0       # 价税合计
    currency: str = "CNY"    # 币种
    raw_text: str = ""       # 原始文本


@dataclass
class ContractClause:
    """合同条款。"""
    clause_id: str = ""      # 条款编号，如 "第三条" / "1"
    title: str = ""          # 条款标题
    content: str = ""        # 条款正文
    clause_type: str = ""    # 条款类型（如 付款/违约/保密/期限）
    line: int = 0            # 起始行号


@dataclass
class RiskPoint:
    """合同风险点。"""
    level: str = "medium"    # 风险等级 high/medium/low
    description: str = ""    # 风险描述
    clause_ref: str = ""     # 关联条款编号
    suggestion: str = ""     # 修改建议
    snippet: str = ""        # 原文片段


@dataclass
class ContractReview:
    """合同审查结果。"""
    clauses: List[ContractClause] = field(default_factory=list)
    risks: List[RiskPoint] = field(default_factory=list)
    compliance_issues: List[str] = field(default_factory=list)
    clause_diffs: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""


@dataclass
class DocAnswer:
    """文档问答结果。"""
    question: str = ""
    answer: str = ""
    source_text: str = ""    # 原文出处片段
    line: int = 0            # 原文行号
    confidence: float = 0.0


@dataclass
class TableCell:
    """表格单元格。"""
    row: int = 0             # 行索引（0 起）
    col: int = 0             # 列索引（0 起）
    text: str = ""           # 单元格文本
    is_header: bool = False  # 是否表头
    is_merged: bool = False  # 是否合并单元格
    rowspan: int = 1         # 跨行数
    colspan: int = 1         # 跨列数


@dataclass
class Table:
    """表格识别结果。"""
    cells: List[TableCell] = field(default_factory=list)
    rows: int = 0            # 总行数
    cols: int = 0            # 总列数
    has_header: bool = False
    header: List[str] = field(default_factory=list)
    merged_cells: List[TableCell] = field(default_factory=list)


@dataclass
class LayoutRegion:
    """版面区域。"""
    region_type: str = "text"  # title/body/image/table/header/footer
    text: str = ""             # 区域文本
    line_start: int = 0        # 起始行
    line_end: int = 0          # 结束行
    confidence: float = 1.0


@dataclass
class LayoutResult:
    """版面分析结果。"""
    regions: List[LayoutRegion] = field(default_factory=list)
    page_count: int = 1
    title: str = ""


# =====================================================================
# 表单识别
# =====================================================================

class FormRecognizer:
    """表单识别器 — 提取键值对、匹配表单模板。"""

    # 键值对分隔符：冒号（中/英）、等号、制表符、两个以上空格
    _KV_PATTERN = re.compile(
        r"^(?P<key>[^\s:：=]{1,30}?)\s*[:：=]\s*(?P<value>.+?)\s*$"
    )
    # "项目  值" 形式（两个以上空格分隔）
    _SPACE_KV_PATTERN = re.compile(
        r"^(?P<key>[^\s]{1,20}?)\s{2,}(?P<value>[^\s].+?)\s*$"
    )

    def extract_fields(self, text: str) -> List[FormField]:
        """从文本中提取表单字段（键值对）。"""
        fields: List[FormField] = []
        if not text:
            return fields
        for idx, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            matched = False
            for pat in (self._KV_PATTERN, self._SPACE_KV_PATTERN):
                m = pat.match(stripped)
                if m:
                    key = m.group("key").strip()
                    value = m.group("value").strip()
                    # 过滤掉明显是正文的长句
                    if 0 < len(key) <= 30 and value:
                        fields.append(FormField(
                            key=key, value=value,
                            confidence=0.85, line=idx,
                        ))
                        matched = True
                        break
            if not matched:
                logger.debug("表单：第 %d 行未识别为键值对: %s", idx, stripped[:40])
        logger.info("表单字段提取完成，共 %d 个字段", len(fields))
        return fields

    def match_template(
        self, text: str, templates: Dict[str, List[str]]
    ) -> Tuple[str, float]:
        """根据关键字匹配表单模板。

        Args:
            text: 表单文本
            templates: {模板名: [关键字列表]}，命中关键字越多置信度越高
        Returns:
            (模板名, 置信度)，未匹配返回 ("", 0.0)
        """
        if not text or not templates:
            return "", 0.0
        best_name, best_score = "", 0.0
        for name, keywords in templates.items():
            if not keywords:
                continue
            hits = sum(1 for kw in keywords if kw in text)
            score = hits / len(keywords)
            if score > best_score:
                best_score, best_name = score, name
        logger.info("模板匹配结果: %s (置信度 %.2f)", best_name, best_score)
        return best_name, best_score

    def recognize(
        self, text: str, templates: Optional[Dict[str, List[str]]] = None
    ) -> Form:
        """完整表单识别：字段提取 + 模板匹配。"""
        fields = self.extract_fields(text)
        template, conf = ("", 0.0)
        if templates:
            template, conf = self.match_template(text, templates)
        return Form(fields=fields, template=template, template_confidence=conf)


# =====================================================================
# 票据识别
# =====================================================================

class ReceiptRecognizer:
    """票据识别器 — 提取发票/收据的结构化信息。"""

    # 金额正则：支持 ¥ ￥ 元 RMB 及千分位逗号
    _MONEY_PATTERN = re.compile(
        r"[¥￥]?\s*([0-9]{1,3}(?:,?[0-9]{3})*(?:\.[0-9]{2})|[0-9]+(?:\.[0-9]{2})?)\s*(?:元|RMB|CNY)?"
    )
    # 日期正则：YYYY-MM-DD / YYYY/MM/DD / YYYY年MM月DD日
    _DATE_PATTERN = re.compile(
        r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?"
    )
    # 税额
    _TAX_PATTERN = re.compile(
        r"(?:税额|税金|税款)\s*[:：]?\s*[¥￥]?\s*([0-9]+(?:\.[0-9]{2})?)"
    )
    # 价税合计
    _TOTAL_PATTERN = re.compile(
        r"(?:价税合计|合计|总计|大写).*?[¥￥]?\s*([0-9]+(?:\.[0-9]{2})?)"
    )
    # 票据号码
    _NO_PATTERN = re.compile(
        r"(?:发票号码|票据号码|单号|编号|No\.?)\s*[:：]?\s*([A-Za-z0-9\-]+)"
    )
    # 商户/开票方
    _MERCHANT_PATTERN = re.compile(
        r"(?:销售方|开票方|收款方|商户|公司名称|名称)\s*[:：]?\s*(.+?)(?:\s|$)"
    )
    # 商品明细行：名称 数量 单价 金额（宽松匹配）
    _ITEM_PATTERN = re.compile(
        r"^(?P<name>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9（）()\/\-]{1,40}?)"
        r"\s+(?P<qty>[0-9]+(?:\.[0-9]+)?)"
        r"\s+(?P<price>[0-9]+(?:\.[0-9]{2})?)"
        r"\s+(?P<amount>[0-9]+(?:\.[0-9]{2})?)\s*$"
    )

    def _to_float(self, s: str) -> float:
        """安全转浮点数，去除千分位逗号。"""
        try:
            return float(s.replace(",", "").strip())
        except (ValueError, AttributeError):
            return 0.0

    def extract_items(self, text: str) -> List[ReceiptItem]:
        """提取商品明细行。"""
        items: List[ReceiptItem] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = self._ITEM_PATTERN.match(line)
            if m:
                items.append(ReceiptItem(
                    name=m.group("name").strip(),
                    quantity=self._to_float(m.group("qty")),
                    unit_price=self._to_float(m.group("price")),
                    amount=self._to_float(m.group("amount")),
                ))
        logger.info("票据明细提取 %d 行", len(items))
        return items

    def extract_amounts(self, text: str) -> Dict[str, float]:
        """提取金额信息（税额、合计、小计）。"""
        result: Dict[str, float] = {"tax": 0.0, "total": 0.0, "subtotal": 0.0}
        m_tax = self._TAX_PATTERN.search(text)
        if m_tax:
            result["tax"] = self._to_float(m_tax.group(1))
        m_total = self._TOTAL_PATTERN.search(text)
        if m_total:
            result["total"] = self._to_float(m_total.group(1))
        # 小计 = 合计 - 税额
        if result["total"] > 0 and result["tax"] >= 0:
            result["subtotal"] = round(result["total"] - result["tax"], 2)
        return result

    def extract_receipt(self, text: str) -> Receipt:
        """完整票据识别，返回结构化结果。"""
        receipt = Receipt(raw_text=text)

        # 日期
        m_date = self._DATE_PATTERN.search(text)
        if m_date:
            y, mo, d = m_date.groups()
            receipt.date = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

        # 票据号码
        m_no = self._NO_PATTERN.search(text)
        if m_no:
            receipt.receipt_no = m_no.group(1).strip()

        # 商户
        m_mer = self._MERCHANT_PATTERN.search(text)
        if m_mer:
            receipt.merchant = m_mer.group(1).strip()

        # 明细与金额
        receipt.items = self.extract_items(text)
        amounts = self.extract_amounts(text)
        receipt.tax = amounts["tax"]
        receipt.total = amounts["total"]
        receipt.subtotal = amounts["subtotal"]

        # 若未提取到合计，则按明细累加
        if receipt.total == 0.0 and receipt.items:
            receipt.subtotal = round(sum(it.amount for it in receipt.items), 2)
            receipt.total = round(receipt.subtotal + receipt.tax, 2)

        logger.info(
            "票据识别完成: 商户=%s 日期=%s 明细=%d 合计=%.2f",
            receipt.merchant, receipt.date, len(receipt.items), receipt.total,
        )
        return receipt


# =====================================================================
# 合同审查
# =====================================================================

class ContractReviewer:
    """合同审查器 — 条款提取、风险检测、条款比对、合规检查。"""

    # 条款标题：第X条 / 一、 / 1. / （一）
    _CLAUSE_PATTERN = re.compile(
        r"^(?:第[一二三四五六七八九十百零\d]+条|[一二三四五六七八九十]+、|"
        r"\d+[\.、]|（[一二三四五六七八九十\d]+）)\s*(?P<title>.*)$"
    )
    # 条款类型关键字
    _CLAUSE_TYPES: Dict[str, List[str]] = {
        "付款": ["付款", "支付", "结算", "价款", "报酬", "费用"],
        "交付": ["交付", "交货", "验收", "提交", "移交"],
        "违约": ["违约", "赔偿", "滞纳金", "罚款", "罚金", "违约金"],
        "解除": ["解除", "终止", "撤销", "中止", "退出"],
        "保密": ["保密", "机密", "泄露", "知识产权"],
        "期限": ["期限", "有效期", "起止", "存续"],
        "不可抗力": ["不可抗力", "意外事件"],
        "争议解决": ["争议", "仲裁", "诉讼", "管辖"],
    }
    # 风险关键字 → (等级, 描述, 建议)
    _RISK_KEYWORDS: List[Tuple[str, str, str, str]] = [
        ("违约金", "high", "包含违约金条款，需关注金额是否过高",
         "建议违约金不超过实际损失的30%"),
        ("无限期", "high", "存在无限期/无期限约定，可能产生长期义务",
         "建议明确合同有效期与终止条件"),
        ("自动续约", "medium", "存在自动续约条款，可能被动延长义务",
         "建议增加提前书面通知解除的机制"),
        ("单方解除", "medium", "存在单方解除权约定，需关注是否对等",
         "建议双方解除条件对等设置"),
        ("不可撤销", "medium", "存在不可撤销承诺，限制后续调整空间",
         "建议保留合理情形下的撤销权"),
        ("最终解释权", "high", "包含最终解释权条款，可能损害己方权益",
         "建议删除或改为双方协商解释"),
        ("滞纳金", "medium", "存在滞纳金条款，需关注计算标准",
         "建议明确滞纳金上限与起算条件"),
        ("概不负责", "high", "存在免责/概不负责表述，可能规避责任",
         "建议明确责任范围，避免概括性免责"),
    ]
    # 合规检查项
    _COMPLIANCE_CHECKS: List[Tuple[str, str]] = [
        ("主体信息", "需包含双方当事人名称/住所等主体信息"),
        ("标的", "需明确合同标的（货物/服务/项目）"),
        ("价款", "需包含价款或报酬及支付方式"),
        ("期限", "需约定合同履行期限"),
        ("违约责任", "需约定违约责任条款"),
        ("争议解决", "需约定争议解决方式（诉讼/仲裁）"),
        ("签署日期", "需包含签署日期或生效日期"),
    ]
    _COMPLIANCE_KEYWORDS: Dict[str, List[str]] = {
        "主体信息": ["甲方", "乙方", "住所", "地址", "法定代表人"],
        "标的": ["标的", "货物", "服务", "项目", "产品"],
        "价款": ["价款", "金额", "报酬", "费用", "支付"],
        "期限": ["期限", "有效期", "履行期", "起止"],
        "违约责任": ["违约", "赔偿", "滞纳金"],
        "争议解决": ["争议", "仲裁", "诉讼", "管辖"],
        "签署日期": ["日期", "年", "月", "日", "签署", "签字", "盖章"],
    }

    def extract_clauses(self, text: str) -> List[ContractClause]:
        """提取合同条款。"""
        clauses: List[ContractClause] = []
        if not text:
            return clauses
        lines = text.splitlines()
        current: Optional[ContractClause] = None
        for idx, line in enumerate(lines, 1):
            m = self._CLAUSE_PATTERN.match(line.strip())
            if m:
                # 保存上一条
                if current is not None:
                    clauses.append(current)
                title = m.group("title").strip()
                clause_id = line.strip().split()[0] if line.strip() else ""
                current = ContractClause(
                    clause_id=clause_id,
                    title=title or clause_id,
                    content=line.strip(),
                    clause_type=self._classify_clause(title + " " + line),
                    line=idx,
                )
            else:
                if current is not None and line.strip():
                    current.content += "\n" + line.strip()
        if current is not None:
            clauses.append(current)
        logger.info("合同条款提取完成，共 %d 条", len(clauses))
        return clauses

    def _classify_clause(self, text: str) -> str:
        """根据关键字判断条款类型。"""
        for ctype, keywords in self._CLAUSE_TYPES.items():
            if any(kw in text for kw in keywords):
                return ctype
        return "其他"

    def detect_risks(self, text: str, clauses: Optional[List[ContractClause]] = None) -> List[RiskPoint]:
        """检测合同风险点。"""
        risks: List[RiskPoint] = []
        # 定位风险所在条款
        clause_lookup = clauses or self.extract_clauses(text)

        for keyword, level, desc, suggestion in self._RISK_KEYWORDS:
            for m in re.finditer(re.escape(keyword), text):
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 30)
                snippet = text[start:end].replace("\n", " ").strip()
                # 查找所属条款
                ref = ""
                pos = m.start()
                for c in clause_lookup:
                    if text.find(c.content.split("\n")[0]) <= pos:
                        ref = c.clause_id
                risks.append(RiskPoint(
                    level=level, description=desc,
                    clause_ref=ref, suggestion=suggestion,
                    snippet=snippet,
                ))
        logger.info("合同风险检测完成，发现 %d 个风险点", len(risks))
        return risks

    def compare_clauses(
        self, clauses_a: List[ContractClause], clauses_b: List[ContractClause]
    ) -> List[Dict[str, Any]]:
        """比对两份合同的条款差异。

        按条款类型匹配，输出新增/删除/修改。
        """
        diffs: List[Dict[str, Any]] = []
        map_a = {c.clause_type: c for c in clauses_a}
        map_b = {c.clause_type: c for c in clauses_b}
        all_types = set(map_a) | set(map_b)
        for ctype in sorted(all_types):
            ca = map_a.get(ctype)
            cb = map_b.get(ctype)
            if ca and not cb:
                diffs.append({
                    "type": "removed", "clause_type": ctype,
                    "clause_id": ca.clause_id, "detail": "乙方版本缺失该条款",
                })
            elif cb and not ca:
                diffs.append({
                    "type": "added", "clause_type": ctype,
                    "clause_id": cb.clause_id, "detail": "甲方版本缺失该条款",
                })
            elif ca and cb and ca.content != cb.content:
                diffs.append({
                    "type": "modified", "clause_type": ctype,
                    "clause_id_a": ca.clause_id, "clause_id_b": cb.clause_id,
                    "detail": "条款内容存在差异",
                })
        logger.info("条款比对完成，共 %d 处差异", len(diffs))
        return diffs

    def check_compliance(self, text: str) -> List[str]:
        """合规性检查，返回缺失项列表。"""
        issues: List[str] = []
        for check_name, _desc in self._COMPLIANCE_CHECKS:
            keywords = self._COMPLIANCE_KEYWORDS.get(check_name, [])
            if not any(kw in text for kw in keywords):
                issues.append(f"缺失合规项：{_desc}")
        logger.info("合规检查完成，发现 %d 项缺失", len(issues))
        return issues

    def review(self, text: str) -> ContractReview:
        """完整合同审查。"""
        clauses = self.extract_clauses(text)
        risks = self.detect_risks(text, clauses)
        compliance = self.check_compliance(text)
        high = sum(1 for r in risks if r.level == "high")
        summary = (
            f"合同共 {len(clauses)} 条条款，发现 {len(risks)} 个风险点"
            f"（高风险 {high} 个），合规缺失 {len(compliance)} 项。"
        )
        logger.info("合同审查总结: %s", summary)
        return ContractReview(
            clauses=clauses, risks=risks,
            compliance_issues=compliance, summary=summary,
        )


# =====================================================================
# 文档问答
# =====================================================================

class DocVQA:
    """文档问答器 — 基于关键词匹配与句子检索回答问题，支持定位原文。"""

    # 句子分隔
    _SENTENCE_SPLIT = re.compile(r"[。！？!?\n；;]+")

    def _tokenize(self, text: str) -> List[str]:
        """简易分词：中文按字 + 英文按词。"""
        tokens: List[str] = []
        # 英文单词
        for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", text):
            tokens.append(w.lower())
        # 中文（去除标点与数字后的连续汉字）
        for seg in re.findall(r"[\u4e00-\u9fa5]+", text):
            # 2-gram 滑窗
            if len(seg) >= 2:
                for i in range(len(seg) - 1):
                    tokens.append(seg[i:i + 2])
            else:
                tokens.append(seg)
        return tokens

    def _score_sentence(self, question_tokens: List[str], sentence: str) -> float:
        """计算问题与句子的相关性分数。"""
        if not question_tokens or not sentence:
            return 0.0
        sent_lower = sentence.lower()
        hits = sum(1 for t in question_tokens if t in sent_lower)
        # 归一化：命中数 / 问题 token 数，避免长句偏向
        return hits / max(len(question_tokens), 1)

    def locate_source(self, question: str, document: str) -> Tuple[str, int]:
        """定位与问题最相关的原文片段，返回 (片段, 行号)。"""
        if not question or not document:
            return "", 0
        q_tokens = self._tokenize(question)
        if not q_tokens:
            return "", 0
        best_sent, best_score, best_line = "", 0.0, 0
        # 按行记录以便返回行号
        for line_idx, line in enumerate(document.splitlines(), 1):
            for sent in self._SENTENCE_SPLIT.split(line):
                sent = sent.strip()
                if len(sent) < 2:
                    continue
                score = self._score_sentence(q_tokens, sent)
                if score > best_score:
                    best_score, best_sent, best_line = score, sent, line_idx
        return best_sent, best_line

    def answer(self, question: str, document: str) -> DocAnswer:
        """基于文档内容回答问题。"""
        source, line = self.locate_source(question, document)
        if not source:
            return DocAnswer(question=question, answer="", confidence=0.0)
        # 简易回答：返回最相关句子，并尝试抽取其中的数值/日期等关键信息
        q_tokens = self._tokenize(question)
        confidence = self._score_sentence(q_tokens, source)
        # 若问题含"多少/金额/数量"等，尝试抽取数值
        answer_text = source
        if re.search(r"多少|金额|数量|价格|费用|总计", question):
            nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", source)
            if nums:
                answer_text = f"{source}（提取数值：{', '.join(nums)}）"
        logger.info("文档问答命中: 行=%d 置信度=%.2f", line, confidence)
        return DocAnswer(
            question=question, answer=answer_text,
            source_text=source, line=line, confidence=confidence,
        )


# =====================================================================
# 表格识别
# =====================================================================

class TableRecognizer:
    """表格识别器 — 支持 Markdown 表格与制表符/多空格对齐表格。"""

    _MARKDOWN_ROW = re.compile(r"^\|(.+)\|\s*$")
    _MARKDOWN_SEP = re.compile(r"^[\s\|:\-]+$")

    def _parse_markdown_table(self, lines: List[str], start: int) -> Tuple[Table, int]:
        """解析 Markdown 表格，返回 (表格, 结束行索引)。"""
        cells: List[TableCell] = []
        header: List[str] = []
        row_idx = 0
        i = start
        end = start
        while i < len(lines):
            line = lines[i]
            m = self._MARKDOWN_ROW.match(line.strip())
            if not m:
                break
            # 分隔行跳过
            if self._MARKDOWN_SEP.match(line.strip()):
                i += 1
                end = i
                continue
            parts = [c.strip() for c in m.group(1).split("|")]
            for col, val in enumerate(parts):
                cells.append(TableCell(
                    row=row_idx, col=col, text=val,
                    is_header=(row_idx == 0),
                ))
            if row_idx == 0:
                header = parts
            row_idx += 1
            i += 1
            end = i
        rows = row_idx
        cols = max((c.col for c in cells), default=-1) + 1 if cells else 0
        table = Table(
            cells=cells, rows=rows, cols=cols,
            has_header=bool(header), header=header,
        )
        return table, end

    def _parse_aligned_table(self, lines: List[str], start: int) -> Tuple[Table, int]:
        """解析制表符/多空格对齐的表格。"""
        cells: List[TableCell] = []
        header: List[str] = []
        row_idx = 0
        i = start
        end = start
        while i < len(lines):
            line = lines[i].rstrip()
            if not line.strip():
                break
            # 按制表符或 2+ 空格切分
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t") if p.strip()]
            else:
                parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
            if len(parts) < 2:
                break
            for col, val in enumerate(parts):
                cells.append(TableCell(
                    row=row_idx, col=col, text=val,
                    is_header=(row_idx == 0),
                ))
            if row_idx == 0:
                header = parts
            row_idx += 1
            i += 1
            end = i
        rows = row_idx
        cols = max((c.col for c in cells), default=-1) + 1 if cells else 0
        table = Table(
            cells=cells, rows=rows, cols=cols,
            has_header=bool(header), header=header,
        )
        return table, end

    def extract_cells(self, text: str) -> List[TableCell]:
        """提取所有表格单元格。"""
        tables = self.recognize(text)
        all_cells: List[TableCell] = []
        for t in tables:
            all_cells.extend(t.cells)
        return all_cells

    def detect_merged_cells(self, table: Table) -> List[TableCell]:
        """检测合并单元格：同一列中相邻空单元格视为被上方合并。"""
        merged: List[TableCell] = []
        # 按列分组
        by_col: Dict[int, List[TableCell]] = {}
        for c in table.cells:
            by_col.setdefault(c.col, []).append(c)
        for col, col_cells in by_col.items():
            col_cells.sort(key=lambda x: x.row)
            for i, c in enumerate(col_cells):
                if not c.text and i > 0:
                    c.is_merged = True
                    c.rowspan = 2
                    merged.append(c)
        table.merged_cells = merged
        return merged

    def recognize(self, text: str) -> List[Table]:
        """识别文本中的所有表格。"""
        tables: List[Table] = []
        if not text:
            return tables
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # Markdown 表格起始
            if self._MARKDOWN_ROW.match(line):
                table, i = self._parse_markdown_table(lines, i)
                if table.rows > 0:
                    self.detect_merged_cells(table)
                    tables.append(table)
                continue
            # 对齐表格起始：含制表符或 2+ 空格且至少 2 列
            if ("\t" in line or re.search(r"\s{2,}", line)) and len(line) > 3:
                parts = [p for p in re.split(r"\t|\s{2,}", line) if p.strip()]
                if len(parts) >= 2:
                    table, i = self._parse_aligned_table(lines, i)
                    if table.rows > 0:
                        self.detect_merged_cells(table)
                        tables.append(table)
                    continue
            i += 1
        logger.info("表格识别完成，共 %d 个表格", len(tables))
        return tables


# =====================================================================
# 版面理解
# =====================================================================

class LayoutAnalyzer:
    """版面分析器 — 将文档划分为标题/正文/表格/页眉页脚等区域。"""

    # 标题：Markdown # / 第X章 / 一、 / 数字编号
    _TITLE_PATTERN = re.compile(
        r"^(?:#{1,6}\s+.+|第[一二三四五六七八九十百零\d]+[章节篇部]|"
        r"[一二三四五六七八九十]+、|\d+\.\s+\S+)$"
    )
    # 页眉页脚：页码 / 仅数字行 / 版权声明
    _PAGE_NUM_PATTERN = re.compile(r"^(?:第\s*\d+\s*页|page\s*\d+|-?\s*\d+\s*-?|共\s*\d+\s*页)", re.IGNORECASE)
    _FOOTER_PATTERN = re.compile(r"(?:版权所有|copyright|©|all rights reserved)", re.IGNORECASE)
    # 表格行
    _TABLE_ROW_PATTERN = re.compile(r"^\|.+\|\s*$")
    # 图片占位
    _IMAGE_PATTERN = re.compile(r"!\[.*?\]\(.+?\)|<img\s|图\s*\d+")

    def classify_line(self, line: str) -> str:
        """分类单行文本，返回区域类型。"""
        stripped = line.strip()
        if not stripped:
            return "blank"
        if self._PAGE_NUM_PATTERN.match(stripped) or self._FOOTER_PATTERN.search(stripped):
            return "footer"
        if self._TITLE_PATTERN.match(stripped):
            return "title"
        if self._TABLE_ROW_PATTERN.match(stripped):
            return "table"
        if self._IMAGE_PATTERN.search(stripped):
            return "image"
        return "body"

    def extract_regions(self, text: str) -> List[LayoutRegion]:
        """提取版面区域，合并相邻同类行。"""
        regions: List[LayoutRegion] = []
        if not text:
            return regions
        lines = text.splitlines()
        cur_type = ""
        cur_start = 0
        cur_lines: List[str] = []

        def flush(end_line: int) -> None:
            if cur_type and cur_type != "blank" and cur_lines:
                regions.append(LayoutRegion(
                    region_type=cur_type,
                    text="\n".join(cur_lines).strip(),
                    line_start=cur_start, line_end=end_line,
                    confidence=0.9,
                ))

        for idx, line in enumerate(lines, 1):
            ltype = self.classify_line(line)
            if ltype == "blank":
                # 空行作为分隔，但不强制结束区域
                cur_lines.append(line)
                continue
            if ltype != cur_type:
                flush(idx - 1)
                cur_type = ltype
                cur_start = idx
                cur_lines = [line]
            else:
                cur_lines.append(line)
        flush(len(lines))
        logger.info("版面区域提取完成，共 %d 个区域", len(regions))
        return regions

    def analyze(self, text: str) -> LayoutResult:
        """完整版面分析。"""
        regions = self.extract_regions(text)
        # 统计页数（按页码标记估算）
        page_count = sum(
            1 for r in regions if r.region_type == "footer"
            and re.search(r"\d+", r.text)
        )
        page_count = max(page_count, 1)
        # 提取文档主标题（第一个标题区域）
        title = ""
        for r in regions:
            if r.region_type == "title":
                title = r.text.split("\n")[0].lstrip("#").strip()
                break
        logger.info("版面分析完成: 页数≈%d 标题=%s", page_count, title[:30])
        return LayoutResult(regions=regions, page_count=page_count, title=title)


# =====================================================================
# 文档智能插件
# =====================================================================

class DocumentIntelligencePlugin(Plugin):
    """文档智能处理插件 — 整合表单/票据/合同/问答/表格/版面能力。"""

    name = "document_intelligence"

    def __init__(self) -> None:
        super().__init__()
        self._form: Optional[FormRecognizer] = None
        self._receipt: Optional[ReceiptRecognizer] = None
        self._contract: Optional[ContractReviewer] = None
        self._vqa: Optional[DocVQA] = None
        self._table: Optional[TableRecognizer] = None
        self._layout: Optional[LayoutAnalyzer] = None
        # 表单模板库（可由配置注入）
        self._form_templates: Dict[str, List[str]] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("document_intelligence", {}) or {}
        # 初始化各识别器
        self._form = FormRecognizer()
        self._receipt = ReceiptRecognizer()
        self._contract = ContractReviewer()
        self._vqa = DocVQA()
        self._table = TableRecognizer()
        self._layout = LayoutAnalyzer()
        # 注入表单模板
        self._form_templates = cfg.get("form_templates", {}) or {}
        logger.info("document_intelligence plugin configured")

    # ------------------------------------------------------- 表单
    def recognize_form(
        self, text: str, templates: Optional[Dict[str, List[str]]] = None
    ) -> Form:
        """表单识别。"""
        if self._form is None:
            self._form = FormRecognizer()
        return self._form.recognize(text, templates or self._form_templates)

    # ------------------------------------------------------- 票据
    def recognize_receipt(self, text: str) -> Receipt:
        """票据识别。"""
        if self._receipt is None:
            self._receipt = ReceiptRecognizer()
        return self._receipt.extract_receipt(text)

    # ------------------------------------------------------- 合同
    def review_contract(self, text: str) -> ContractReview:
        """合同审查。"""
        if self._contract is None:
            self._contract = ContractReviewer()
        return self._contract.review(text)

    def compare_contracts(self, text_a: str, text_b: str) -> List[Dict[str, Any]]:
        """比对两份合同条款差异。"""
        if self._contract is None:
            self._contract = ContractReviewer()
        clauses_a = self._contract.extract_clauses(text_a)
        clauses_b = self._contract.extract_clauses(text_b)
        return self._contract.compare_clauses(clauses_a, clauses_b)

    # ------------------------------------------------------- 文档问答
    def ask_document(self, question: str, document: str) -> DocAnswer:
        """基于文档内容回答问题。"""
        if self._vqa is None:
            self._vqa = DocVQA()
        return self._vqa.answer(question, document)

    # ------------------------------------------------------- 表格
    def recognize_tables(self, text: str) -> List[Table]:
        """表格识别。"""
        if self._table is None:
            self._table = TableRecognizer()
        return self._table.recognize(text)

    # ------------------------------------------------------- 版面
    def analyze_layout(self, text: str) -> LayoutResult:
        """版面分析。"""
        if self._layout is None:
            self._layout = LayoutAnalyzer()
        return self._layout.analyze(text)

    # ------------------------------------------------------- 统一入口
    def process(
        self, text: str, task: str = "layout", **kwargs: Any
    ) -> Any:
        """统一处理入口，按任务类型分发。

        Args:
            text: 文档文本
            task: 任务类型 form/receipt/contract/vqa/table/layout
            **kwargs: 任务相关参数（如 vqa 的 question）
        """
        task = (task or "").lower()
        logger.info("文档智能处理任务: %s", task)
        if task == "form":
            return self.recognize_form(text, kwargs.get("templates"))
        if task == "receipt":
            return self.recognize_receipt(text)
        if task == "contract":
            return self.review_contract(text)
        if task == "vqa":
            return self.ask_document(kwargs.get("question", ""), text)
        if task == "table":
            return self.recognize_tables(text)
        if task == "layout":
            return self.analyze_layout(text)
        logger.warning("未知文档智能任务: %s，回退到版面分析", task)
        return self.analyze_layout(text)

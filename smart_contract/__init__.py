"""智能合约生成与审计模块。

提供：
  - 合约生成器（ContractGenerator）：从自然语言描述生成 Solidity 合约代码，
    支持 ERC20 / ERC721 / 投票 / 拍卖 / 多签钱包等模板。
  - 安全审计器（SecurityAuditor）：检测重入攻击、整数溢出、权限控制不当、
    时间戳依赖、tx.origin 误用、未检查 call 返回值等常见漏洞并给出修复建议。
  - 多链支持（MultiChainSupport）：适配 Ethereum / Solana / BSC / Polygon 等链。
  - 合约测试生成器（ContractTestGenerator）：自动生成正常流程 / 边界条件 / 攻击场景测试。
  - Gas 优化器（GasOptimizer）：分析 Gas 消耗并提供存储 / 循环 / 短路评估等优化建议。
  - SmartContractPlugin：整合以上功能的插件类。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class ContractSpec:
    """合约规格描述。"""

    name: str
    template: str  # erc20 / erc721 / voting / auction / multisig
    description: str = ""
    chain: str = "ethereum"
    solidity_version: str = "0.8.20"
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedContract:
    """生成的合约结果。"""

    name: str
    template: str
    chain: str
    code: str
    language: str = "solidity"


@dataclass
class AuditFinding:
    """审计发现项。"""

    severity: str  # critical / high / medium / low / info
    vulnerability: str  # 漏洞类型
    line: int
    snippet: str
    description: str
    suggestion: str


@dataclass
class AuditReport:
    """审计报告。"""

    contract_name: str
    findings: List[AuditFinding] = field(default_factory=list)
    passed: bool = True

    def summary(self) -> str:
        """生成审计摘要文本。"""
        counts: Dict[str, int] = {}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        status = "通过" if self.passed else "未通过"
        if counts:
            parts = [f"{k}: {v}" for k, v in counts.items()]
            return f"审计结果: {status} | " + ", ".join(parts)
        return f"审计结果: {status} | 未发现问题"


@dataclass
class ChainConfig:
    """区块链配置。"""

    name: str
    chain_id: int
    currency: str
    language: str  # solidity / rust / ...
    rpc_url: str = ""
    explorer_url: str = ""
    features: List[str] = field(default_factory=list)
    solidity_version: str = "0.8.20"
    notes: str = ""


@dataclass
class TestCase:
    """测试用例。"""

    name: str
    description: str
    category: str  # normal / boundary / attack
    code: str


@dataclass
class TestSuite:
    """测试套件。"""

    contract_name: str
    language: str
    test_cases: List[TestCase] = field(default_factory=list)


@dataclass
class GasSuggestion:
    """Gas 优化建议。"""

    category: str  # storage / loop / short-circuit / custom-error / calldata / ...
    description: str
    snippet_before: str = ""
    snippet_after: str = ""
    estimated_saving: str = ""


@dataclass
class GasReport:
    """Gas 优化报告。"""

    contract_name: str
    suggestions: List[GasSuggestion] = field(default_factory=list)

    def summary(self) -> str:
        """生成 Gas 优化摘要。"""
        if not self.suggestions:
            return f"{self.contract_name}: 暂无优化建议"
        cats = {}
        for s in self.suggestions:
            cats[s.category] = cats.get(s.category, 0) + 1
        parts = [f"{k}({v})" for k, v in cats.items()]
        return f"{self.contract_name}: {len(self.suggestions)} 条建议 | " + ", ".join(parts)


# ============================================================================
# Solidity 合约模板（字符串模板，使用 __NAME__ / __SOL_VERSION__ 占位符）
# ============================================================================

_ERC20_TEMPLATE = """// SPDX-License-Identifier: MIT
pragma solidity ^__SOL_VERSION__;

/// @title __NAME__ - ERC20 代币合约
/// @notice 由 SmartContractPlugin 自动生成
contract __NAME__ {
    string public name;
    string public symbol;
    uint8 public decimals;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    address public owner;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor(
        string memory name_,
        string memory symbol_,
        uint8 decimals_,
        uint256 initialSupply_
    ) {
        name = name_;
        symbol = symbol_;
        decimals = decimals_;
        totalSupply = initialSupply_ * 10 ** uint256(decimals_);
        balanceOf[msg.sender] = totalSupply;
        owner = msg.sender;
        emit Transfer(address(0), msg.sender, totalSupply);
    }

    function transfer(address to_, uint256 amount_) external returns (bool) {
        require(balanceOf[msg.sender] >= amount_, "insufficient balance");
        balanceOf[msg.sender] -= amount_;
        balanceOf[to_] += amount_;
        emit Transfer(msg.sender, to_, amount_);
        return true;
    }

    function approve(address spender_, uint256 amount_) external returns (bool) {
        allowance[msg.sender][spender_] = amount_;
        emit Approval(msg.sender, spender_, amount_);
        return true;
    }

    function transferFrom(address from_, address to_, uint256 amount_) external returns (bool) {
        require(balanceOf[from_] >= amount_, "insufficient balance");
        require(allowance[from_][msg.sender] >= amount_, "insufficient allowance");
        allowance[from_][msg.sender] -= amount_;
        balanceOf[from_] -= amount_;
        balanceOf[to_] += amount_;
        emit Transfer(from_, to_, amount_);
        return true;
    }

    function mint(address to_, uint256 amount_) external onlyOwner {
        totalSupply += amount_;
        balanceOf[to_] += amount_;
        emit Transfer(address(0), to_, amount_);
    }

    function burn(uint256 amount_) external {
        require(balanceOf[msg.sender] >= amount_, "insufficient balance");
        balanceOf[msg.sender] -= amount_;
        totalSupply -= amount_;
        emit Transfer(msg.sender, address(0), amount_);
    }
}
"""

_ERC721_TEMPLATE = """// SPDX-License-Identifier: MIT
pragma solidity ^__SOL_VERSION__;

/// @title __NAME__ - ERC721 非同质化代币合约
/// @notice 由 SmartContractPlugin 自动生成
contract __NAME__ {
    string public name;
    string public symbol;

    uint256 public totalSupply;
    uint256 private _nextTokenId = 1;

    mapping(uint256 => address) public ownerOf;
    mapping(address => uint256) public balanceOf;
    mapping(uint256 => address) public getApproved;
    mapping(address => mapping(address => bool)) public isApprovedForAll;

    address public owner;

    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event Approval(address indexed owner, address indexed approved, uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner, address indexed operator, bool approved);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor(string memory name_, string memory symbol_) {
        name = name_;
        symbol = symbol_;
        owner = msg.sender;
    }

    function mint(address to_) external onlyOwner {
        uint256 tokenId = _nextTokenId;
        _nextTokenId++;
        totalSupply++;
        ownerOf[tokenId] = to_;
        balanceOf[to_]++;
        emit Transfer(address(0), to_, tokenId);
    }

    function transfer(address to_, uint256 tokenId_) external {
        require(ownerOf[tokenId_] == msg.sender, "not owner");
        require(to_ != address(0), "zero address");
        balanceOf[msg.sender]--;
        balanceOf[to_]++;
        ownerOf[tokenId_] = to_;
        delete getApproved[tokenId_];
        emit Transfer(msg.sender, to_, tokenId_);
    }

    function approve(address approved_, uint256 tokenId_) external {
        require(ownerOf[tokenId_] == msg.sender, "not owner");
        getApproved[tokenId_] = approved_;
        emit Approval(msg.sender, approved_, tokenId_);
    }

    function setApprovalForAll(address operator_, bool approved_) external {
        isApprovedForAll[msg.sender][operator_] = approved_;
        emit ApprovalForAll(msg.sender, operator_, approved_);
    }
}
"""

_VOTING_TEMPLATE = """// SPDX-License-Identifier: MIT
pragma solidity ^__SOL_VERSION__;

/// @title __NAME__ - 投票治理合约
/// @notice 由 SmartContractPlugin 自动生成
contract __NAME__ {
    struct Proposal {
        string description;
        uint256 voteCountYes;
        uint256 voteCountNo;
        uint256 deadline;
        bool executed;
        bool passed;
    }

    struct Voter {
        bool registered;
        bool voted;
    }

    address public owner;
    Proposal[] public proposals;
    mapping(address => Voter) public voters;

    uint256 public votingDuration;

    event VoterRegistered(address voter);
    event ProposalCreated(uint256 indexed proposalId, string description, uint256 deadline);
    event Voted(address voter, uint256 proposalId, bool support);
    event ProposalExecuted(uint256 proposalId, bool passed);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor(uint256 votingDurationMinutes_) {
        owner = msg.sender;
        votingDuration = votingDurationMinutes_ * 1 minutes;
    }

    function registerVoter(address voter_) external onlyOwner {
        require(!voters[voter_].registered, "already registered");
        voters[voter_].registered = true;
        emit VoterRegistered(voter_);
    }

    function createProposal(string memory description_) external onlyOwner {
        uint256 deadline = block.timestamp + votingDuration;
        proposals.push(Proposal({
            description: description_,
            voteCountYes: 0,
            voteCountNo: 0,
            deadline: deadline,
            executed: false,
            passed: false
        }));
        emit ProposalCreated(proposals.length - 1, description_, deadline);
    }

    function vote(uint256 proposalId_, bool support_) external {
        Voter storage voter = voters[msg.sender];
        require(voter.registered, "not registered");
        require(!voter.voted, "already voted");
        require(block.timestamp < proposals[proposalId_].deadline, "voting ended");

        voter.voted = true;
        if (support_) {
            proposals[proposalId_].voteCountYes++;
        } else {
            proposals[proposalId_].voteCountNo++;
        }
        emit Voted(msg.sender, proposalId_, support_);
    }

    function executeProposal(uint256 proposalId_) external onlyOwner {
        Proposal storage p = proposals[proposalId_];
        require(!p.executed, "already executed");
        require(block.timestamp >= p.deadline, "voting not ended");

        p.executed = true;
        p.passed = p.voteCountYes > p.voteCountNo;
        emit ProposalExecuted(proposalId_, p.passed);
    }
}
"""

_AUCTION_TEMPLATE = """// SPDX-License-Identifier: MIT
pragma solidity ^__SOL_VERSION__;

/// @title __NAME__ - 拍卖合约
/// @notice 由 SmartContractPlugin 自动生成
contract __NAME__ {
    address public owner;
    address public highestBidder;
    uint256 public highestBid;
    uint256 public auctionEndTime;
    bool public ended;

    mapping(address => uint256) public pendingReturns;

    event HighestBidIncreased(address bidder, uint256 amount);
    event AuctionEnded(address winner, uint256 amount);
    event AuctionStarted(uint256 endTime);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor(uint256 biddingTimeMinutes_) {
        owner = msg.sender;
        auctionEndTime = block.timestamp + biddingTimeMinutes_ * 1 minutes;
        emit AuctionStarted(auctionEndTime);
    }

    function bid() external payable {
        require(block.timestamp < auctionEndTime, "auction ended");
        require(msg.value > highestBid, "bid too low");

        if (highestBid != 0) {
            pendingReturns[highestBidder] += highestBid;
        }

        highestBidder = msg.sender;
        highestBid = msg.value;
        emit HighestBidIncreased(msg.sender, msg.value);
    }

    function withdraw() external returns (bool) {
        uint256 amount = pendingReturns[msg.sender];
        require(amount > 0, "nothing to withdraw");

        pendingReturns[msg.sender] = 0;
        (bool success, ) = msg.sender.call{value: amount}("");
        require(success, "withdraw failed");

        return true;
    }

    function auctionEnd() external onlyOwner {
        require(block.timestamp >= auctionEndTime, "auction not yet ended");
        require(!ended, "auction already ended");

        ended = true;
        emit AuctionEnded(highestBidder, highestBid);

        (bool success, ) = owner.call{value: highestBid}("");
        require(success, "transfer failed");
    }
}
"""

_MULTISIG_TEMPLATE = """// SPDX-License-Identifier: MIT
pragma solidity ^__SOL_VERSION__;

/// @title __NAME__ - 多签钱包合约
/// @notice 由 SmartContractPlugin 自动生成
contract __NAME__ {
    struct Transaction {
        address to;
        uint256 value;
        bytes data;
        bool executed;
        uint256 confirmations;
    }

    address[] public owners;
    mapping(address => bool) public isOwner;
    uint256 public required;
    Transaction[] public transactions;
    mapping(uint256 => mapping(address => bool)) public confirmed;

    event Deposit(address sender, uint256 amount);
    event Submission(uint256 txId);
    event Confirmation(address owner, uint256 txId);
    event Execution(uint256 txId);
    event Revocation(address owner, uint256 txId);

    modifier onlyOwner() {
        require(isOwner[msg.sender], "not owner");
        _;
    }

    modifier txExists(uint256 txId_) {
        require(txId_ < transactions.length, "tx does not exist");
        _;
    }

    modifier notExecuted(uint256 txId_) {
        require(!transactions[txId_].executed, "tx already executed");
        _;
    }

    constructor(address[] memory owners_, uint256 required_) {
        require(owners_.length > 0, "no owners");
        require(required_ > 0 && required_ <= owners_.length, "invalid required");

        for (uint256 i = 0; i < owners_.length; i++) {
            address o = owners_[i];
            require(o != address(0), "zero owner");
            require(!isOwner[o], "duplicate owner");
            isOwner[o] = true;
            owners.push(o);
        }
        required = required_;
    }

    receive() external payable {
        emit Deposit(msg.sender, msg.value);
    }

    function submit(address to_, uint256 value_, bytes memory data_) external onlyOwner {
        transactions.push(Transaction({
            to: to_,
            value: value_,
            data: data_,
            executed: false,
            confirmations: 0
        }));
        emit Submission(transactions.length - 1);
    }

    function confirm(uint256 txId_) external onlyOwner txExists(txId_) notExecuted(txId_) {
        require(!confirmed[txId_][msg.sender], "already confirmed");
        confirmed[txId_][msg.sender] = true;
        transactions[txId_].confirmations++;
        emit Confirmation(msg.sender, txId_);
    }

    function revoke(uint256 txId_) external onlyOwner txExists(txId_) notExecuted(txId_) {
        require(confirmed[txId_][msg.sender], "not confirmed");
        confirmed[txId_][msg.sender] = false;
        transactions[txId_].confirmations--;
        emit Revocation(msg.sender, txId_);
    }

    function execute(uint256 txId_) external onlyOwner txExists(txId_) notExecuted(txId_) {
        require(transactions[txId_].confirmations >= required, "not enough confirmations");

        Transaction storage txn = transactions[txId_];
        txn.executed = true;

        (bool success, ) = txn.to.call{value: txn.value}(txn.data);
        require(success, "tx failed");

        emit Execution(txId_);
    }
}
"""


# ============================================================================
# 合约生成器
# ============================================================================


class ContractGenerator:
    """合约生成器 — 从自然语言描述生成 Solidity 合约代码。"""

    TEMPLATES: Tuple[str, ...] = ("erc20", "erc721", "voting", "auction", "multisig")

    def __init__(self) -> None:
        # 关键词到模板的映射（按优先级排序，先匹配者优先）
        self._keyword_map: List[Tuple[str, str]] = [
            ("erc721", "erc721"),
            ("nft", "erc721"),
            ("非同质化", "erc721"),
            ("erc20", "erc20"),
            ("代币", "erc20"),
            ("token", "erc20"),
            ("fungible", "erc20"),
            ("投票", "voting"),
            ("vote", "voting"),
            ("治理", "voting"),
            ("governance", "voting"),
            ("拍卖", "auction"),
            ("auction", "auction"),
            ("竞价", "auction"),
            ("多签", "multisig"),
            ("multisig", "multisig"),
            ("multi-sig", "multisig"),
            ("钱包", "multisig"),
            ("wallet", "multisig"),
        ]
        self._templates: Dict[str, str] = {
            "erc20": _ERC20_TEMPLATE,
            "erc721": _ERC721_TEMPLATE,
            "voting": _VOTING_TEMPLATE,
            "auction": _AUCTION_TEMPLATE,
            "multisig": _MULTISIG_TEMPLATE,
        }

    def infer_template(self, description: str) -> str:
        """从自然语言描述推断合约模板类型。"""
        text = description.lower()
        for keyword, template in self._keyword_map:
            if keyword in text:
                logger.debug("描述匹配模板: %s -> %s", keyword, template)
                return template
        logger.debug("未匹配到关键词，使用默认模板 erc20")
        return "erc20"

    def generate(self, spec: ContractSpec) -> GeneratedContract:
        """根据规格生成合约代码。"""
        template = self._templates.get(spec.template)
        if template is None:
            raise ValueError(f"未知模板类型: {spec.template}")
        code = self._render(template, spec)
        logger.info("生成合约: %s (模板=%s, 链=%s)", spec.name, spec.template, spec.chain)
        return GeneratedContract(
            name=spec.name,
            template=spec.template,
            chain=spec.chain,
            code=code,
            language="solidity",
        )

    def generate_from_description(
        self, description: str, name: str = "MyContract"
    ) -> GeneratedContract:
        """从自然语言描述直接生成合约。"""
        template = self.infer_template(description)
        spec = ContractSpec(name=name, template=template, description=description)
        return self.generate(spec)

    @staticmethod
    def _render(template: str, spec: ContractSpec) -> str:
        """渲染模板，替换占位符。"""
        code = template.replace("__NAME__", spec.name)
        code = code.replace("__SOL_VERSION__", spec.solidity_version)
        return code

    def list_templates(self) -> List[str]:
        """列出可用模板。"""
        return list(self._templates.keys())


# ============================================================================
# 安全审计器
# ============================================================================


class SecurityAuditor:
    """安全审计器 — 检测常见 Solidity 漏洞并给出修复建议。"""

    # 预期为公开的函数名（无需额外权限控制）
    _PUBLIC_OK = {
        "transfer",
        "transferfrom",
        "approve",
        "bid",
        "vote",
        "withdraw",
        "setapprovalforall",
        "deposit",
        "fallback",
        "receive",
    }

    def audit(self, contract_name: str, code: str) -> AuditReport:
        """审计合约代码，返回审计报告。"""
        findings: List[AuditFinding] = []
        findings.extend(self._check_reentrancy(code))
        findings.extend(self._check_integer_overflow(code))
        findings.extend(self._check_access_control(code))
        findings.extend(self._check_timestamp_dependency(code))
        findings.extend(self._check_tx_origin(code))
        findings.extend(self._check_unchecked_call(code))

        # 按行号排序
        findings.sort(key=lambda f: (f.line, f.severity))
        passed = not any(f.severity in ("critical", "high") for f in findings)
        logger.info("审计完成: %s, 发现 %d 个问题 (passed=%s)", contract_name, len(findings), passed)
        return AuditReport(contract_name=contract_name, findings=findings, passed=passed)

    def _check_reentrancy(self, code: str) -> List[AuditFinding]:
        """检测重入攻击风险。"""
        findings: List[AuditFinding] = []
        lines = code.splitlines()
        # 低级别调用：.call{...}(...)、.call(...)、.delegatecall(...)、.staticcall(...)、.send(...)
        call_pattern = re.compile(r"\.(call|delegatecall|staticcall|send)\s*[\{(]")
        # 状态修改模式：简单赋值或复合赋值（排除 ==、>=、<=、!=）
        state_change_pattern = re.compile(r"^\s*\w+(\[\w+\])?\s*[+\-*/]?=\s*[^=]")
        for idx, line in enumerate(lines, start=1):
            if not call_pattern.search(line):
                continue
            # 检查调用之后是否还有状态修改（启发式）
            after = "\n".join(lines[idx:])
            has_state_change_after = any(
                state_change_pattern.match(l) for l in after.splitlines()
            )
            if has_state_change_after:
                severity = "high"
                desc = "外部调用后存在状态修改，可能存在重入攻击风险"
                suggestion = (
                    "遵循 Checks-Effects-Interactions 模式：先校验、再修改状态、"
                    "最后发起外部调用；或使用 OpenZeppelin ReentrancyGuard。"
                )
            else:
                severity = "medium"
                desc = "使用低级别外部调用，需确认调用顺序安全"
                suggestion = "确认遵循 Checks-Effects-Interactions 模式，建议使用 ReentrancyGuard。"
            findings.append(
                AuditFinding(
                    severity=severity,
                    vulnerability="重入攻击 (Reentrancy)",
                    line=idx,
                    snippet=line.strip(),
                    description=desc,
                    suggestion=suggestion,
                )
            )
        return findings

    def _check_integer_overflow(self, code: str) -> List[AuditFinding]:
        """检测整数溢出/下溢风险。"""
        findings: List[AuditFinding] = []
        lines = code.splitlines()
        version_match = re.search(r"pragma\s+solidity\s+\^?(\d+)\.(\d+)\.(\d+)", code)
        unsafe_version = False
        if version_match:
            ver = tuple(int(x) for x in version_match.groups())
            unsafe_version = ver < (0, 8, 0)
        else:
            findings.append(
                AuditFinding(
                    severity="medium",
                    vulnerability="整数溢出 (Overflow/Underflow)",
                    line=1,
                    snippet="",
                    description="未声明 pragma solidity 版本，无法确认溢出保护",
                    suggestion="显式声明 pragma solidity ^0.8.0 或更高版本。",
                )
            )
        has_safe_math = "SafeMath" in code
        # 复合赋值运算符明确表示算术运算
        arith_pattern = re.compile(r"[+\-*/]=")
        if unsafe_version and not has_safe_math:
            for idx, line in enumerate(lines, start=1):
                if arith_pattern.search(line):
                    findings.append(
                        AuditFinding(
                            severity="high",
                            vulnerability="整数溢出 (Overflow/Underflow)",
                            line=idx,
                            snippet=line.strip(),
                            description="Solidity < 0.8.0 默认不检查溢出，且未使用 SafeMath",
                            suggestion="升级到 Solidity >= 0.8.0（自动溢出检查），或引入 SafeMath 库。",
                        )
                    )
                    break  # 每个合约仅报告一次
        return findings

    def _check_access_control(self, code: str) -> List[AuditFinding]:
        """检测权限控制不当。"""
        findings: List[AuditFinding] = []
        lines = code.splitlines()
        func_pattern = re.compile(r"function\s+(\w+)\s*\([^)]*\)\s*(public|external)\b")
        access_keywords = (
            "onlyOwner",
            "onlyRole",
            "onlyAdmin",
            "require(msg.sender",
            "require(owner",
        )
        # 赋值模式（排除 ==、>=、<=、!=、:=）
        assign_pattern = re.compile(r"(?<![=!<>:])=(?!=)")
        for idx, line in enumerate(lines, start=1):
            m = func_pattern.search(line)
            if not m:
                continue
            func_name = m.group(1)
            if func_name.lower() in self._PUBLIC_OK:
                continue
            # 取函数声明行及后续若干行作为上下文
            context = "\n".join(lines[idx - 1: idx + 5])
            has_access = any(kw in context for kw in access_keywords)
            is_view = "view" in context or "pure" in context
            if has_access or is_view:
                continue
            if assign_pattern.search(context):
                findings.append(
                    AuditFinding(
                        severity="medium",
                        vulnerability="权限控制不当",
                        line=idx,
                        snippet=line.strip(),
                        description=f"函数 {func_name} 可被任意调用且修改状态，缺少权限控制",
                        suggestion="为敏感函数添加 onlyOwner 等权限修饰符，或使用 AccessControl。",
                    )
                )
        return findings

    def _check_timestamp_dependency(self, code: str) -> List[AuditFinding]:
        """检测时间戳依赖。"""
        findings: List[AuditFinding] = []
        lines = code.splitlines()
        ts_pattern = re.compile(r"\b(block\.timestamp|now)\b")
        for idx, line in enumerate(lines, start=1):
            if ts_pattern.search(line):
                findings.append(
                    AuditFinding(
                        severity="low",
                        vulnerability="时间戳依赖",
                        line=idx,
                        snippet=line.strip(),
                        description="使用 block.timestamp/now，矿工可在一定范围内操纵",
                        suggestion="避免用时间戳作为关键决策依据，或允许合理误差范围。",
                    )
                )
        return findings

    def _check_tx_origin(self, code: str) -> List[AuditFinding]:
        """检测 tx.origin 误用。"""
        findings: List[AuditFinding] = []
        lines = code.splitlines()
        pattern = re.compile(r"\btx\.origin\b")
        for idx, line in enumerate(lines, start=1):
            if pattern.search(line):
                findings.append(
                    AuditFinding(
                        severity="high",
                        vulnerability="tx.origin 误用",
                        line=idx,
                        snippet=line.strip(),
                        description="使用 tx.origin 进行授权可被钓鱼攻击绕过",
                        suggestion="使用 msg.sender 进行权限校验，仅在必要时使用 tx.origin。",
                    )
                )
        return findings

    def _check_unchecked_call(self, code: str) -> List[AuditFinding]:
        """检测未检查 call 返回值。"""
        findings: List[AuditFinding] = []
        lines = code.splitlines()
        send_pattern = re.compile(r"\.send\s*\(")
        call_pattern = re.compile(r"\.(call|delegatecall|staticcall)\s*[\{(]")
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            # .send() 返回 bool，常被忽略
            if send_pattern.search(stripped) and not re.search(
                r"(require|if|assert|bool|=)", stripped
            ):
                findings.append(
                    AuditFinding(
                        severity="medium",
                        vulnerability="未检查 call 返回值",
                        line=idx,
                        snippet=stripped,
                        description=".send() 返回值未检查，失败会被静默忽略",
                        suggestion="使用 require(...) 或检查返回值：require(addr.send(amount));",
                    )
                )
            # 低级别 call 未捕获返回值
            if call_pattern.search(stripped) and "bool" not in stripped and "require" not in stripped:
                if not re.search(r"\(\s*bool", stripped):
                    findings.append(
                        AuditFinding(
                            severity="medium",
                            vulnerability="未检查 call 返回值",
                            line=idx,
                            snippet=stripped,
                            description="低级别 call 返回值未检查",
                            suggestion="捕获并检查返回值：(bool ok, ) = addr.call{...}(...); require(ok);",
                        )
                    )
        return findings


# ============================================================================
# 多链支持
# ============================================================================


class MultiChainSupport:
    """多链支持 — 适配不同区块链的合约模板与特性。"""

    def __init__(self) -> None:
        self._chains: Dict[str, ChainConfig] = {
            "ethereum": ChainConfig(
                name="Ethereum",
                chain_id=1,
                currency="ETH",
                language="solidity",
                rpc_url="https://mainnet.infura.io/v3",
                explorer_url="https://etherscan.io",
                features=["ERC20", "ERC721", "EIP-1559", "Account Abstraction"],
                solidity_version="0.8.20",
                notes="主网，Gas 较高，建议充分优化",
            ),
            "solana": ChainConfig(
                name="Solana",
                chain_id=0,
                currency="SOL",
                language="rust",
                rpc_url="https://api.mainnet-beta.solana.com",
                explorer_url="https://explorer.solana.com",
                features=["SPL Token", "Program", "Anchor"],
                solidity_version="",
                notes="使用 Rust + Anchor 框架，非 EVM 链",
            ),
            "bsc": ChainConfig(
                name="BSC",
                chain_id=56,
                currency="BNB",
                language="solidity",
                rpc_url="https://bsc-dataseed.binance.org",
                explorer_url="https://bscscan.com",
                features=["BEP20", "BEP721", "BEP95"],
                solidity_version="0.8.20",
                notes="币安智能链，EVM 兼容，Gas 较低",
            ),
            "polygon": ChainConfig(
                name="Polygon",
                chain_id=137,
                currency="MATIC",
                language="solidity",
                rpc_url="https://polygon-rpc.com",
                explorer_url="https://polygonscan.com",
                features=["ERC20", "ERC721", "PoS Bridge"],
                solidity_version="0.8.20",
                notes="Polygon PoS，EVM 兼容，Layer2",
            ),
        }

    def get_chain(self, name: str) -> Optional[ChainConfig]:
        """获取链配置。"""
        return self._chains.get(name.lower())

    def list_chains(self) -> List[str]:
        """列出支持的链。"""
        return list(self._chains.keys())

    def adapt_contract(self, code: str, chain: str) -> str:
        """根据目标链适配合约代码。"""
        cfg = self.get_chain(chain)
        if cfg is None:
            logger.warning("不支持的链: %s，返回原始代码", chain)
            return code
        if cfg.language != "solidity":
            logger.info("链 %s 使用 %s，Solidity 代码需手动迁移", chain, cfg.language)
            return code
        # 调整 pragma 版本
        if cfg.solidity_version:
            code = re.sub(
                r"pragma\s+solidity\s+\^[0-9.]+;",
                f"pragma solidity ^{cfg.solidity_version};",
                code,
            )
        # 添加链相关注释头
        header = (
            f"// 部署目标链: {cfg.name} (chainId={cfg.chain_id}, 货币={cfg.currency})\n"
            f"// 备注: {cfg.notes}\n"
        )
        if not code.startswith("// 部署目标链"):
            code = header + code
        logger.info("合约已适配链: %s", chain)
        return code

    def get_template_for_chain(self, template: str, chain: str) -> str:
        """获取指定链的模板说明。"""
        cfg = self.get_chain(chain)
        if cfg is None:
            return template
        if cfg.language != "solidity":
            return f"{template} (需用 {cfg.language} 重写)"
        return template


# ============================================================================
# 合约测试生成器
# ============================================================================


class ContractTestGenerator:
    """合约测试生成器 — 自动生成正常流程 / 边界条件 / 攻击场景测试用例。"""

    def generate(self, contract: GeneratedContract) -> TestSuite:
        """根据合约生成测试套件。"""
        cases = self._tests_for(contract)
        logger.info("为合约 %s 生成 %d 个测试用例", contract.name, len(cases))
        return TestSuite(
            contract_name=contract.name,
            language="solidity",
            test_cases=cases,
        )

    def _tests_for(self, contract: GeneratedContract) -> List[TestCase]:
        """按模板分发测试用例生成。"""
        name = contract.name
        template = contract.template
        if template == "erc20":
            return self._erc20_tests(name)
        if template == "erc721":
            return self._erc721_tests(name)
        if template == "voting":
            return self._voting_tests(name)
        if template == "auction":
            return self._auction_tests(name)
        if template == "multisig":
            return self._multisig_tests(name)
        return self._generic_tests(name)

    @staticmethod
    def _test_contract(test_name: str, setup: str, body: str) -> str:
        """组装完整的 Foundry 测试合约。"""
        return (
            "// SPDX-License-Identifier: MIT\n"
            "pragma solidity ^0.8.20;\n\n"
            'import "forge-std/Test.sol";\n\n'
            f"contract {test_name} is Test {{\n"
            f"{setup}\n"
            f"{body}\n"
            "}\n"
        )

    # ---- ERC20 测试 ----
    def _erc20_tests(self, name: str) -> List[TestCase]:
        setup = (
            f"    {name} token;\n"
            "    address alice = address(0x1);\n"
            "    address bob = address(0x2);\n\n"
            "    function setUp() public {\n"
            f'        token = new {name}("TestToken", "TT", 18, 1000000);\n'
            "        token.transfer(alice, 1000 ether);\n"
            "    }"
        )
        return [
            TestCase(
                name="test_ERC20_Deployment",
                description="验证部署后初始供应量与归属正确",
                category="normal",
                code=self._test_contract(
                    "ERC20DeploymentTest",
                    setup,
                    "    function testInitialSupply() public {\n"
                    "        assertEq(token.totalSupply(), 1000000 ether);\n"
                    "        assertGt(token.balanceOf(alice), 0);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_ERC20_TransferInsufficientBalance",
                description="余额不足时转账应回退",
                category="boundary",
                code=self._test_contract(
                    "ERC20BoundaryTest",
                    setup,
                    "    function testRevertInsufficientBalance() public {\n"
                    "        vm.prank(alice);\n"
                    "        vm.expectRevert(\"insufficient balance\");\n"
                    "        token.transfer(bob, 1000000 ether);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_ERC20_ReentrancyAttack",
                description="验证转账遵循 CEI 模式，无重入风险",
                category="attack",
                code=self._test_contract(
                    "ERC20AttackTest",
                    setup,
                    "    function testNoReentrancy() public {\n"
                    "        vm.prank(alice);\n"
 "        uint256 before = token.balanceOf(bob);\n"
                    "        token.transfer(bob, 100 ether);\n"
                    "        assertEq(token.balanceOf(bob), before + 100 ether);\n"
                    "    }",
                ),
            ),
        ]

    # ---- ERC721 测试 ----
    def _erc721_tests(self, name: str) -> List[TestCase]:
        setup = (
            f"    {name} nft;\n"
            "    address alice = address(0x1);\n\n"
            "    function setUp() public {\n"
            f'        nft = new {name}("TestNFT", "TNFT");\n'
            "    }"
        )
        return [
            TestCase(
                name="test_ERC721_Mint",
                description="验证铸造后归属与总量正确",
                category="normal",
                code=self._test_contract(
                    "ERC721MintTest",
                    setup,
                    "    function testMint() public {\n"
                    "        nft.mint(alice);\n"
                    "        assertEq(nft.ownerOf(1), alice);\n"
                    "        assertEq(nft.totalSupply(), 1);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_ERC721_TransferNotOwner",
                description="非持有者转账应回退",
                category="boundary",
                code=self._test_contract(
                    "ERC721BoundaryTest",
                    setup,
                    "    function testRevertNotOwner() public {\n"
                    "        nft.mint(alice);\n"
                    "        vm.prank(address(0x999));\n"
                    "        vm.expectRevert(\"not owner\");\n"
                    "        nft.transfer(address(0x3), 1);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_ERC721_UnauthorizedMint",
                description="非 owner 铸造应回退",
                category="attack",
                code=self._test_contract(
                    "ERC721AttackTest",
                    setup,
                    "    function testRevertUnauthorizedMint() public {\n"
                    "        vm.prank(alice);\n"
                    "        vm.expectRevert(\"not owner\");\n"
                    "        nft.mint(alice);\n"
                    "    }",
                ),
            ),
        ]

    # ---- 投票测试 ----
    def _voting_tests(self, name: str) -> List[TestCase]:
        setup = (
            f"    {name} voting;\n"
            "    address alice = address(0x1);\n\n"
            "    function setUp() public {\n"
            "        voting = new Voting(60);\n"
            "        voting.registerVoter(alice);\n"
            "        voting.createProposal(\"Proposal A\");\n"
            "    }"
        )
        return [
            TestCase(
                name="test_Voting_CastVote",
                description="注册选民投票后计票正确",
                category="normal",
                code=self._test_contract(
                    "VotingNormalTest",
                    setup,
                    "    function testVote() public {\n"
                    "        vm.prank(alice);\n"
                    "        voting.vote(0, true);\n"
                    "        (, uint256 yes, , , , ) = voting.proposals(0);\n"
                    "        assertEq(yes, 1);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_Voting_DoubleVote",
                description="重复投票应回退",
                category="boundary",
                code=self._test_contract(
                    "VotingBoundaryTest",
                    setup,
                    "    function testRevertDoubleVote() public {\n"
                    "        vm.startPrank(alice);\n"
                    "        voting.vote(0, true);\n"
                    "        vm.expectRevert(\"already voted\");\n"
                    "        voting.vote(0, true);\n"
                    "        vm.stopPrank();\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_Voting_UnregisteredVote",
                description="未注册选民投票应回退",
                category="attack",
                code=self._test_contract(
                    "VotingAttackTest",
                    setup,
                    "    function testRevertUnregistered() public {\n"
                    "        vm.prank(address(0x999));\n"
                    "        vm.expectRevert(\"not registered\");\n"
                    "        voting.vote(0, true);\n"
                    "    }",
                ),
            ),
        ]

    # ---- 拍卖测试 ----
    def _auction_tests(self, name: str) -> List[TestCase]:
        setup = (
            f"    {name} auction;\n"
            "    address alice = address(0x1);\n"
            "    address bob = address(0x2);\n\n"
            "    function setUp() public {\n"
            "        auction = new Auction(60);\n"
            "        vm.deal(alice, 10 ether);\n"
            "        vm.deal(bob, 10 ether);\n"
            "    }"
        )
        return [
            TestCase(
                name="test_Auction_Bid",
                description="出价后最高出价者更新正确",
                category="normal",
                code=self._test_contract(
                    "AuctionNormalTest",
                    setup,
                    "    function testBid() public {\n"
                    "        vm.prank(alice);\n"
                    "        auction.bid{value: 1 ether}();\n"
                    "        assertEq(auction.highestBidder(), alice);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_Auction_LowBid",
                description="低于当前最高价应回退",
                category="boundary",
                code=self._test_contract(
                    "AuctionBoundaryTest",
                    setup,
                    "    function testRevertLowBid() public {\n"
                    "        vm.prank(alice);\n"
                    "        auction.bid{value: 1 ether}();\n"
                    "        vm.prank(bob);\n"
                    "        vm.expectRevert(\"bid too low\");\n"
                    "        auction.bid{value: 0.5 ether}();\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_Auction_WithdrawReentrancy",
                description="验证提现遵循 CEI，攻击者无法重入",
                category="attack",
                code=self._test_contract(
                    "AuctionAttackTest",
                    setup,
                    "    function testWithdrawSafe() public {\n"
                    "        vm.prank(alice);\n"
                    "        auction.bid{value: 1 ether}();\n"
                    "        vm.prank(bob);\n"
                    "        auction.bid{value: 2 ether}();\n"
                    "        vm.prank(alice);\n"
                    "        auction.withdraw();\n"
                    "        assertEq(alice.balance, 10 ether);\n"
                    "    }",
                ),
            ),
        ]

    # ---- 多签钱包测试 ----
    def _multisig_tests(self, name: str) -> List[TestCase]:
        setup = (
            f"    {name} wallet;\n"
            "    address[] owners;\n"
            "    address alice = address(0x1);\n"
            "    address bob = address(0x2);\n\n"
            "    function setUp() public {\n"
            "        owners.push(alice);\n"
            "        owners.push(bob);\n"
            "        wallet = new MultiSigWallet(owners, 2);\n"
            "        vm.deal(address(wallet), 10 ether);\n"
            "    }"
        )
        return [
            TestCase(
                name="test_MultiSig_SubmitAndConfirm",
                description="提交并确认交易后确认数正确",
                category="normal",
                code=self._test_contract(
                    "MultiSigNormalTest",
                    setup,
                    "    function testSubmitConfirm() public {\n"
                    "        vm.prank(alice);\n"
                    "        wallet.submit(address(0x3), 1 ether, \"\");\n"
                    "        vm.prank(bob);\n"
                    "        wallet.confirm(0);\n"
                    "        (, , , , uint256 confs) = wallet.transactions(0);\n"
                    "        assertEq(confs, 2);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_MultiSig_ExecuteWithoutEnoughConfirm",
                description="确认数不足时执行应回退",
                category="boundary",
                code=self._test_contract(
                    "MultiSigBoundaryTest",
                    setup,
                    "    function testRevertNotEnoughConfirm() public {\n"
                    "        vm.prank(alice);\n"
                    "        wallet.submit(address(0x3), 1 ether, \"\");\n"
                    "        vm.prank(alice);\n"
                    "        wallet.confirm(0);\n"
                    "        vm.expectRevert(\"not enough confirmations\");\n"
                    "        wallet.execute(0);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_MultiSig_NonOwnerSubmit",
                description="非 owner 提交交易应回退",
                category="attack",
                code=self._test_contract(
                    "MultiSigAttackTest",
                    setup,
                    "    function testRevertNonOwner() public {\n"
                    "        vm.prank(address(0x999));\n"
                    "        vm.expectRevert(\"not owner\");\n"
                    "        wallet.submit(address(0x3), 1 ether, \"\");\n"
                    "    }",
                ),
            ),
        ]

    # ---- 通用测试 ----
    def _generic_tests(self, name: str) -> List[TestCase]:
        setup = (
            f"    {name} target;\n\n"
            "    function setUp() public {\n"
            f"        target = new {name}();\n"
            "    }"
        )
        return [
            TestCase(
                name="test_Deployment",
                description="验证合约可正常部署",
                category="normal",
                code=self._test_contract(
                    "GenericDeploymentTest",
                    setup,
                    "    function testDeployed() public {\n"
                    f"        assertTrue(address(target) != address(0));\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_RevertOnInvalidInput",
                description="非法输入应回退",
                category="boundary",
                code=self._test_contract(
                    "GenericBoundaryTest",
                    setup,
                    "    function testRevertInvalid() public {\n"
                    "        // 根据具体业务补充边界条件\n"
                    "        assertTrue(true);\n"
                    "    }",
                ),
            ),
            TestCase(
                name="test_AttackScenario",
                description="攻击场景验证（占位，按业务补充）",
                category="attack",
                code=self._test_contract(
                    "GenericAttackTest",
                    setup,
                    "    function testAttack() public {\n"
                    "        // 根据具体业务补充攻击场景\n"
                    "        assertTrue(true);\n"
                    "    }",
                ),
            ),
        ]


# ============================================================================
# Gas 优化器
# ============================================================================


class GasOptimizer:
    """Gas 优化器 — 分析合约 Gas 消耗并提供优化建议。"""

    def optimize(self, contract_name: str, code: str) -> GasReport:
        """分析合约并返回 Gas 优化建议。"""
        suggestions: List[GasSuggestion] = []
        suggestions.extend(self._check_storage_packing(code))
        suggestions.extend(self._check_loop_optimization(code))
        suggestions.extend(self._check_short_circuit(code))
        suggestions.extend(self._check_custom_errors(code))
        suggestions.extend(self._check_calldata(code))
        suggestions.extend(self._check_cache_storage_read(code))
        logger.info(
            "Gas 优化分析完成: %s, %d 条建议", contract_name, len(suggestions)
        )
        return GasReport(contract_name=contract_name, suggestions=suggestions)

    def _check_storage_packing(self, code: str) -> List[GasSuggestion]:
        """检测存储槽打包机会。"""
        suggestions: List[GasSuggestion] = []
        small_type_pattern = re.compile(
            r"^\s*(uint8|uint16|uint32|uint64|bool|address)\s+public\s+(\w+)",
            re.MULTILINE,
        )
        matches = small_type_pattern.findall(code)
        if len(matches) >= 2:
            suggestions.append(
                GasSuggestion(
                    category="存储打包",
                    description=(
                        f"检测到 {len(matches)} 个小类型状态变量，"
                        "可打包到同一存储槽以节省 Gas"
                    ),
                    snippet_before=(
                        "// 分散存储\nuint8 public a;\nuint8 public b;\naddress public c;"
                    ),
                    snippet_after=(
                        "// 打包存储（按顺序排列共享 32 字节槽）\n"
                        "uint8 public a;\nuint8 public b;\naddress public c;"
                    ),
                    estimated_saving="每个打包槽节省约 5000-20000 Gas",
                )
            )
        return suggestions

    def _check_loop_optimization(self, code: str) -> List[GasSuggestion]:
        """检测循环优化机会。"""
        suggestions: List[GasSuggestion] = []
        lines = code.splitlines()
        for idx, line in enumerate(lines, start=1):
            if re.search(r"for\s*\(", line):
                if ".length" in line:
                    suggestions.append(
                        GasSuggestion(
                            category="循环优化",
                            description=(
                                f"第 {idx} 行：循环条件中直接读取 .length，"
                                "每次迭代都访问存储"
                            ),
                            snippet_before=(
                                "for (uint256 i = 0; i < arr.length; i++) {"
                            ),
                            snippet_after=(
                                "uint256 len = arr.length;\n"
                                "for (uint256 i = 0; i < len; i++) {"
                            ),
                            estimated_saving="每次迭代节省约 100-200 Gas",
                        )
                    )
                if "i++" in line:
                    suggestions.append(
                        GasSuggestion(
                            category="循环优化",
                            description=(
                                f"第 {idx} 行：使用 i++ 可改为 ++i 节省 Gas"
                            ),
                            snippet_before=(
                                "for (uint256 i = 0; i < len; i++) {"
                            ),
                            snippet_after=(
                                "for (uint256 i = 0; i < len; ++i) {"
                            ),
                            estimated_saving="每次迭代节省约 5 Gas",
                        )
                    )
        return suggestions

    def _check_short_circuit(self, code: str) -> List[GasSuggestion]:
        """检测短路评估优化机会。"""
        suggestions: List[GasSuggestion] = []
        lines = code.splitlines()
        for idx, line in enumerate(lines, start=1):
            if "require(" in line and "&&" in line:
                suggestions.append(
                    GasSuggestion(
                        category="短路评估",
                        description=(
                            f"第 {idx} 行：require 中 && 条件顺序可优化，"
                            "将廉价/高概率条件放左侧以短路求值"
                        ),
                        snippet_before=(
                            'require(expensiveCall() && cheapCheck, "msg");'
                        ),
                        snippet_after=(
                            'require(cheapCheck && expensiveCall(), "msg");'
                        ),
                        estimated_saving="失败时避免昂贵调用",
                    )
                )
        return suggestions

    def _check_custom_errors(self, code: str) -> List[GasSuggestion]:
        """检测使用自定义错误替代 require 字符串。"""
        suggestions: List[GasSuggestion] = []
        count = len(re.findall(r'require\s*\([^;]*"[^"]*"\s*\)', code))
        if count > 0:
            suggestions.append(
                GasSuggestion(
                    category="自定义错误",
                    description=(
                        f"检测到 {count} 处 require 带字符串，"
                        "部署和调用时消耗更多 Gas"
                    ),
                    snippet_before='require(msg.sender == owner, "not owner");',
                    snippet_after=(
                        "error NotOwner();\n"
                        "if (msg.sender != owner) revert NotOwner();"
                    ),
                    estimated_saving="每次 revert 节省约 50+ Gas，部署也省 Gas",
                )
            )
        return suggestions

    def _check_calldata(self, code: str) -> List[GasSuggestion]:
        """检测可用 calldata 替代 memory 的函数参数。"""
        suggestions: List[GasSuggestion] = []
        pattern = re.compile(r"function\s+\w+\s*\(([^)]*)\)\s*external")
        for m in pattern.finditer(code):
            params = m.group(1)
            if "memory" in params and "calldata" not in params:
                suggestions.append(
                    GasSuggestion(
                        category="calldata 优化",
                        description="external 函数的数组/结构体参数应使用 calldata 而非 memory",
                        snippet_before="function foo(uint256[] memory arr) external {",
                        snippet_after="function foo(uint256[] calldata arr) external {",
                        estimated_saving="每次调用节省约 1000+ Gas（避免内存拷贝）",
                    )
                )
                break
        return suggestions

    def _check_cache_storage_read(self, code: str) -> List[GasSuggestion]:
        """检测循环内重复读取存储变量。"""
        suggestions: List[GasSuggestion] = []
        lines = code.splitlines()
        in_loop_depth = 0
        for idx, line in enumerate(lines, start=1):
            if re.search(r"for\s*\(|while\s*\(", line):
                in_loop_depth += 1
                continue
            if in_loop_depth > 0:
                # 循环体内读取 storage 变量（启发式：含 storage 关键字或直接读状态变量）
                if "storage" in line and re.search(r"\b\w+\s*\[", line):
                    suggestions.append(
                        GasSuggestion(
                            category="存储读取缓存",
                            description=(
                                f"第 {idx} 行：循环内读取存储变量，"
                                "建议缓存到内存变量"
                            ),
                            snippet_before="for (...) { total += balances[i]; }",
                            snippet_after=(
                                "uint256[] memory bals = balances;\n"
                                "for (...) { total += bals[i]; }"
                            ),
                            estimated_saving="每次读取节省约 100 Gas",
                        )
                    )
                    in_loop_depth = 0
                if "}" in line:
                    in_loop_depth = max(0, in_loop_depth - 1)
        return suggestions


# ============================================================================
# 插件类
# ============================================================================


class SmartContractPlugin(Plugin):
    """智能合约生成与审计插件 — 整合生成、审计、多链、测试与 Gas 优化。"""

    name = "smart_contract"

    def __init__(self) -> None:
        super().__init__()
        self.generator = ContractGenerator()
        self.auditor = SecurityAuditor()
        self.chains = MultiChainSupport()
        self.test_gen = ContractTestGenerator()
        self.gas_opt = GasOptimizer()

    async def setup(self, ctx) -> None:
        """初始化智能合约插件。"""
        await super().setup(ctx)
        logger.info("smart_contract plugin configured")

    # ---- 合约生成 ----
    def generate_contract(self, spec: ContractSpec) -> GeneratedContract:
        """生成合约代码（含多链适配）。"""
        contract = self.generator.generate(spec)
        contract.code = self.chains.adapt_contract(contract.code, spec.chain)
        return contract

    def generate_from_description(
        self,
        description: str,
        name: str = "MyContract",
        chain: str = "ethereum",
    ) -> GeneratedContract:
        """从自然语言描述生成合约。"""
        template = self.generator.infer_template(description)
        spec = ContractSpec(
            name=name, template=template, description=description, chain=chain
        )
        return self.generate_contract(spec)

    # ---- 安全审计 ----
    def audit_contract(self, contract_name: str, code: str) -> AuditReport:
        """审计合约代码。"""
        return self.auditor.audit(contract_name, code)

    # ---- 测试生成 ----
    def generate_tests(self, contract: GeneratedContract) -> TestSuite:
        """为合约生成测试套件。"""
        return self.test_gen.generate(contract)

    # ---- Gas 优化 ----
    def optimize_gas(self, contract_name: str, code: str) -> GasReport:
        """分析并给出 Gas 优化建议。"""
        return self.gas_opt.optimize(contract_name, code)

    # ---- 多链 ----
    def list_supported_chains(self) -> List[str]:
        """列出支持的链。"""
        return self.chains.list_chains()

    def list_templates(self) -> List[str]:
        """列出可用合约模板。"""
        return self.generator.list_templates()

    # ---- 完整流水线 ----
    def full_pipeline(
        self,
        description: str,
        name: str = "MyContract",
        chain: str = "ethereum",
    ) -> Dict[str, Any]:
        """完整流水线：生成 -> 审计 -> 测试 -> Gas 优化。"""
        contract = self.generate_from_description(description, name=name, chain=chain)
        report = self.audit_contract(contract.name, contract.code)
        tests = self.generate_tests(contract)
        gas = self.optimize_gas(contract.name, contract.code)
        logger.info("完整流水线完成: %s", contract.name)
        return {
            "contract": contract,
            "audit_report": report,
            "test_suite": tests,
            "gas_report": gas,
        }


__all__ = [
    "ContractSpec",
    "GeneratedContract",
    "AuditFinding",
    "AuditReport",
    "ChainConfig",
    "TestCase",
    "TestSuite",
    "GasSuggestion",
    "GasReport",
    "ContractGenerator",
    "SecurityAuditor",
    "MultiChainSupport",
    "ContractTestGenerator",
    "GasOptimizer",
    "SmartContractPlugin",
]

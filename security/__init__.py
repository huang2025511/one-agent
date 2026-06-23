"""安全性增强模块 — 提供加密、安全审计和漏洞扫描功能。

提供：
  - 数据加密和解密
  - API密钥管理
  - 安全审计日志
  - 漏洞扫描
  - 输入验证和清理
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class SecurityEvent:
    """安全事件类。"""
    event_id: str
    event_type: str  # authentication / authorization / vulnerability / audit
    severity: str  # low / medium / high / critical
    message: str
    timestamp: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VulnerabilityReport:
    """漏洞报告类。"""
    id: str
    severity: str
    title: str
    description: str
    package: str
    version: str
    fixed_version: str = ""
    cve_id: str = ""
    remediation: str = ""


class EncryptionManager:
    """加密管理器 — 提供数据加密和解密功能。"""

    def __init__(self, secret_key: str = None):
        self._secret_key = secret_key or self._generate_key()
        self._key = hashlib.sha256(self._secret_key.encode()).digest()

    @staticmethod
    def _generate_key() -> str:
        """生成随机密钥。"""
        return secrets.token_hex(32)

    def encrypt(self, data: str) -> str:
        """加密字符串数据。"""
        import cryptography
        from cryptography.fernet import Fernet
        
        # 使用密钥派生
        fernet_key = base64.urlsafe_b64encode(self._key[:32])
        f = Fernet(fernet_key)
        return f.encrypt(data.encode()).decode()

    def decrypt(self, encrypted_data: str) -> str:
        """解密字符串数据。"""
        import cryptography
        from cryptography.fernet import Fernet
        
        fernet_key = base64.urlsafe_b64encode(self._key[:32])
        f = Fernet(fernet_key)
        return f.decrypt(encrypted_data.encode()).decode()

    def generate_api_key(self, prefix: str = "sk") -> str:
        """生成安全的API密钥。"""
        return f"{prefix}_{secrets.token_hex(32)}"

    def hash_password(self, password: str, salt: str = None) -> tuple:
        """哈希密码。"""
        if salt is None:
            salt = secrets.token_hex(16)
        hashed = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode(),
            salt.encode(),
            100000
        )
        return salt, hashed.hex()

    def verify_password(self, password: str, salt: str, hashed_password: str) -> bool:
        """验证密码。"""
        _, hashed = self.hash_password(password, salt)
        return hmac.compare_digest(hashed, hashed_password)


class InputValidator:
    """输入验证器 — 验证和清理用户输入。"""

    # 危险模式匹配
    _DANGEROUS_PATTERNS = [
        re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F]'),  # 控制字符
        re.compile(r'(\.\./|\.\.)'),  # 路径遍历
        re.compile(r'([\'"])\s*OR\s*\1.*?=\1'),  # SQL注入
        re.compile(r'<script[^>]*>.*?</script>', re.IGNORECASE),  # XSS
        re.compile(r'javascript:', re.IGNORECASE),  # JS协议
    ]

    @staticmethod
    def validate_string(value: str, max_length: int = 10000) -> bool:
        """验证字符串输入。"""
        if not isinstance(value, str):
            return False
        if len(value) > max_length:
            return False
        for pattern in InputValidator._DANGEROUS_PATTERNS:
            if pattern.search(value):
                return False
        return True

    @staticmethod
    def sanitize_input(value: str) -> str:
        """清理用户输入，移除危险内容。"""
        if not isinstance(value, str):
            return str(value)
        
        # 移除控制字符
        value = ''.join(c for c in value if ord(c) >= 32 or c == '\n' or c == '\t')
        
        # 移除路径遍历
        value = re.sub(r'\.\./', '', value)
        
        # HTML转义
        value = value.replace('&', '&amp;')
        value = value.replace('<', '&lt;')
        value = value.replace('>', '&gt;')
        value = value.replace('"', '&quot;')
        value = value.replace("'", '&#39;')
        
        return value

    @staticmethod
    def validate_email(email: str) -> bool:
        """验证邮箱格式。"""
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))

    @staticmethod
    def validate_url(url: str) -> bool:
        """验证URL格式。"""
        pattern = r'^https?://[a-zA-Z0-9.-]+(?:/[a-zA-Z0-9._~:/?#\\[\\]@!$&\'()*+,;=-]*)?$'
        return bool(re.match(pattern, url))


class VulnerabilityScanner:
    """漏洞扫描器 — 检测依赖包中的已知漏洞。"""

    def __init__(self):
        self._vulnerabilities: List[VulnerabilityReport] = []

    def scan_packages(self) -> List[VulnerabilityReport]:
        """扫描项目依赖包中的漏洞。"""
        self._vulnerabilities = []
        
        try:
            # 使用 pip-audit 扫描
            result = subprocess.run(
                ["pip-audit", "--json"],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    for pkg in data.get("packages", []):
                        for vuln in pkg.get("vulnerabilities", []):
                            self._vulnerabilities.append(VulnerabilityReport(
                                id=vuln.get("id", ""),
                                severity=vuln.get("severity", "medium"),
                                title=vuln.get("description", ""),
                                description=vuln.get("details", ""),
                                package=pkg.get("name", ""),
                                version=pkg.get("version", ""),
                                fixed_version=vuln.get("fixed_versions", [""])[0] if vuln.get("fixed_versions") else "",
                                cve_id=vuln.get("cve_id", ""),
                                remediation=vuln.get("fix", "")
                            ))
                except json.JSONDecodeError:
                    pass
            elif result.returncode == 2:
                # pip-audit 返回码 2 表示发现漏洞但执行成功
                try:
                    data = json.loads(result.stdout)
                    for pkg in data.get("packages", []):
                        for vuln in pkg.get("vulnerabilities", []):
                            self._vulnerabilities.append(VulnerabilityReport(
                                id=vuln.get("id", ""),
                                severity=vuln.get("severity", "medium"),
                                title=vuln.get("description", ""),
                                description=vuln.get("details", ""),
                                package=pkg.get("name", ""),
                                version=pkg.get("version", ""),
                                fixed_version=vuln.get("fixed_versions", [""])[0] if vuln.get("fixed_versions") else "",
                                cve_id=vuln.get("cve_id", ""),
                                remediation=vuln.get("fix", "")
                            ))
                except json.JSONDecodeError:
                    pass
        except FileNotFoundError:
            logger.warning("pip-audit not installed, skipping vulnerability scan")
        except subprocess.TimeoutExpired:
            logger.warning("Vulnerability scan timed out")
        except Exception as exc:
            logger.warning("Vulnerability scan failed: %s", exc)
        
        return self._vulnerabilities

    def get_severity_counts(self) -> Dict[str, int]:
        """按严重程度统计漏洞数量。"""
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for vuln in self._vulnerabilities:
            if vuln.severity.lower() in counts:
                counts[vuln.severity.lower()] += 1
        return counts


class SecurityAudit:
    """安全审计器 — 记录和查询安全事件。"""

    def __init__(self, log_path: str = "data/security"):
        self._log_path = Path(log_path)
        self._log_path.mkdir(parents=True, exist_ok=True)
        self._events: List[SecurityEvent] = []

    def log_event(self, event_type: str, severity: str, message: str, details: Dict[str, Any] = None):
        """记录安全事件。"""
        event = SecurityEvent(
            event_id=f"sec_{int(time.time())}_{secrets.token_hex(8)}",
            event_type=event_type,
            severity=severity,
            message=message,
            details=details or {}
        )
        self._events.append(event)
        
        # 写入日志文件
        log_file = self._log_path / f"security_{time.strftime('%Y%m%d')}.json"
        log_data = []
        if log_file.exists():
            try:
                log_data = json.loads(log_file.read_text(encoding='utf-8'))
            except Exception:
                pass
        
        log_data.append({
            "event_id": event.event_id,
            "event_type": event.event_type,
            "severity": event.severity,
            "message": event.message,
            "timestamp": event.timestamp,
            "details": event.details
        })
        
        log_file.write_text(json.dumps(log_data, indent=2, ensure_ascii=False), encoding='utf-8')
        
        logger.info("Security event: [%s] %s - %s", severity, event_type, message)

    def query_events(self, event_type: str = None, severity: str = None, limit: int = 100) -> List[SecurityEvent]:
        """查询安全事件。"""
        results = self._events
        
        if event_type:
            results = [e for e in results if e.event_type == event_type]
        if severity:
            results = [e for e in results if e.severity == severity]
        
        return sorted(results, key=lambda x: x.timestamp, reverse=True)[:limit]

    def get_summary(self) -> Dict[str, Any]:
        """获取安全摘要。"""
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        types = {}
        
        for event in self._events:
            if event.severity.lower() in counts:
                counts[event.severity.lower()] += 1
            types[event.event_type] = types.get(event.event_type, 0) + 1
        
        return {
            "total_events": len(self._events),
            "by_severity": counts,
            "by_type": types
        }


class SecurityPlugin(Plugin):
    """安全性增强插件。"""

    name = "security"

    def __init__(self):
        super().__init__()
        self._encryption = None
        self._validator = InputValidator()
        self._scanner = VulnerabilityScanner()
        self._audit = None

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("security", {}) or {}
        
        secret_key = cfg.get("secret_key", "")
        if not secret_key:
            secret_key = secrets.token_hex(32)
            logger.warning("No secret key configured, generated a new one")
        
        self._encryption = EncryptionManager(secret_key)
        self._audit = SecurityAudit(cfg.get("log_path", "data/security"))
        
        logger.info("Security plugin configured")

    def get_encryption(self) -> EncryptionManager:
        """获取加密管理器。"""
        return self._encryption

    def get_validator(self) -> InputValidator:
        """获取输入验证器。"""
        return self._validator

    def get_scanner(self) -> VulnerabilityScanner:
        """获取漏洞扫描器。"""
        return self._scanner

    def get_audit(self) -> SecurityAudit:
        """获取安全审计器。"""
        return self._audit

    async def run_security_check(self) -> Dict[str, Any]:
        """运行完整的安全检查。"""
        vulnerabilities = self._scanner.scan_packages()
        summary = self._audit.get_summary()
        
        return {
            "vulnerabilities": [v.__dict__ for v in vulnerabilities],
            "severity_counts": self._scanner.get_severity_counts(),
            "audit_summary": summary,
            "timestamp": time.time()
        }

    def sanitize_input(self, value: str) -> str:
        """清理用户输入（便捷方法）。"""
        return self._validator.sanitize_input(value)
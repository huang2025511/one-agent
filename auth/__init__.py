"""
基于角色的访问控制（RBAC）模块

提供角色定义、权限管理、用户-角色映射和权限检查功能。
支持权限持久化到SQLite，支持角色继承。
"""

import sqlite3
import threading
from typing import List, Optional, Set, Dict


class Permission:
    """权限定义类"""
    
    def __init__(self, name: str, description: str = ""):
        """
        初始化权限
        
        Args:
            name: 权限名称
            description: 权限描述
        """
        self.name = name
        self.description = description


class Role:
    """角色定义类"""
    
    def __init__(self, name: str, description: str = "", inherit_from: Optional[str] = None):
        """
        初始化角色
        
        Args:
            name: 角色名称
            description: 角色描述
            inherit_from: 继承的父角色名称
        """
        self.name = name
        self.description = description
        self.inherit_from = inherit_from


class UserRole:
    """用户角色关联类"""
    
    def __init__(self, user_id: str, role_name: str):
        """
        初始化用户角色关联
        
        Args:
            user_id: 用户ID
            role_name: 角色名称
        """
        self.user_id = user_id
        self.role_name = role_name


class RBACManager:
    """RBAC管理器类"""
    
    # 内置权限定义
    BUILTIN_PERMISSIONS = [
        Permission("chat", "聊天权限"),
        Permission("manage_users", "用户管理"),
        Permission("manage_settings", "设置管理"),
        Permission("manage_skills", "技能管理"),
        Permission("manage_workflows", "工作流管理"),
        Permission("export_data", "数据导出"),
        Permission("view_audit", "审计日志查看"),
    ]
    
    # 内置角色定义（含继承关系）
    BUILTIN_ROLES = [
        Role("admin", "管理员（所有权限）"),
        Role("user", "普通用户（基本操作）", inherit_from="guest"),
        Role("guest", "访客（只读访问）"),
        Role("api", "API客户端（API访问）", inherit_from="user"),
    ]
    
    # 角色默认权限配置
    ROLE_PERMISSIONS = {
        "admin": ["chat", "manage_users", "manage_settings", "manage_skills", 
                  "manage_workflows", "export_data", "view_audit"],
        "user": ["chat"],
        "guest": [],
        "api": ["chat"],
    }
    
    def __init__(self, db_path: str = "auth.db"):
        """
        初始化RBAC管理器
        
        Args:
            db_path: SQLite数据库文件路径
        """
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = None
        self._init_connection()
        self._init_database_with_data()
    
    def _init_connection(self):
        """初始化数据库连接"""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
    
    def _get_connection(self):
        """获取数据库连接"""
        if self._conn is None:
            self._init_connection()
        return self._conn
    
    def _init_database_with_data(self) -> None:
        """初始化数据库表结构和内置数据"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 创建权限表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS permissions (
                    name TEXT PRIMARY KEY,
                    description TEXT
                )
            """)
            
            # 创建角色表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS roles (
                    name TEXT PRIMARY KEY,
                    description TEXT,
                    inherit_from TEXT,
                    FOREIGN KEY (inherit_from) REFERENCES roles(name)
                )
            """)
            
            # 创建角色权限关联表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS role_permissions (
                    role_name TEXT,
                    permission_name TEXT,
                    PRIMARY KEY (role_name, permission_name),
                    FOREIGN KEY (role_name) REFERENCES roles(name),
                    FOREIGN KEY (permission_name) REFERENCES permissions(name)
                )
            """)
            
            # 创建用户角色关联表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id TEXT,
                    role_name TEXT,
                    PRIMARY KEY (user_id, role_name),
                    FOREIGN KEY (role_name) REFERENCES roles(name)
                )
            """)
            
            # 插入内置权限
            for perm in self.BUILTIN_PERMISSIONS:
                cursor.execute("INSERT OR IGNORE INTO permissions (name, description) VALUES (?, ?)",
                              (perm.name, perm.description))
            
            # 插入内置角色（按继承顺序）
            for role in self.BUILTIN_ROLES:
                cursor.execute("INSERT OR IGNORE INTO roles (name, description, inherit_from) VALUES (?, ?, ?)",
                              (role.name, role.description, role.inherit_from))
            
            # 插入角色权限
            for role_name, perms in self.ROLE_PERMISSIONS.items():
                for perm_name in perms:
                    cursor.execute("INSERT OR IGNORE INTO role_permissions (role_name, permission_name) VALUES (?, ?)",
                                  (role_name, perm_name))
            
            conn.commit()
    
    def _init_database(self) -> None:
        """初始化数据库表结构（已弃用，使用 _init_database_with_data）"""
        self._init_database_with_data()
    
    def _init_builtin_data(self) -> None:
        """初始化内置权限和角色（已弃用，使用 _init_database_with_data）"""
        pass
    
    def add_permission(self, name: str, description: str = "") -> None:
        """
        添加新权限
        
        Args:
            name: 权限名称
            description: 权限描述
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO permissions (name, description) VALUES (?, ?)",
                          (name, description))
            conn.commit()
            
    
    def remove_permission(self, name: str) -> None:
        """
        删除权限
        
        Args:
            name: 权限名称
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM permissions WHERE name = ?", (name,))
            cursor.execute("DELETE FROM role_permissions WHERE permission_name = ?", (name,))
            conn.commit()
            
    
    def list_permissions(self) -> List[Permission]:
        """
        获取所有权限列表
        
        Returns:
            权限对象列表
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name, description FROM permissions")
            permissions = [Permission(name, description) for name, description in cursor.fetchall()]
            
            return permissions
    
    def add_role(self, name: str, description: str = "", inherit_from: Optional[str] = None) -> None:
        """
        添加新角色
        
        Args:
            name: 角色名称
            description: 角色描述
            inherit_from: 继承的父角色名称
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO roles (name, description, inherit_from) VALUES (?, ?, ?)",
                          (name, description, inherit_from))
            conn.commit()
            
    
    def remove_role(self, name: str) -> None:
        """
        删除角色
        
        Args:
            name: 角色名称
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM roles WHERE name = ?", (name,))
            cursor.execute("DELETE FROM role_permissions WHERE role_name = ?", (name,))
            cursor.execute("DELETE FROM user_roles WHERE role_name = ?", (name,))
            conn.commit()
            
    
    def list_roles(self) -> List[Role]:
        """
        获取所有角色列表
        
        Returns:
            角色对象列表
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name, description, inherit_from FROM roles")
            roles = [Role(name, description, inherit_from) for name, description, inherit_from in cursor.fetchall()]
            
            return roles
    
    def add_role_permission(self, role_name: str, permission_name: str) -> None:
        """
        为角色添加权限
        
        Args:
            role_name: 角色名称
            permission_name: 权限名称
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO role_permissions (role_name, permission_name) VALUES (?, ?)",
                          (role_name, permission_name))
            conn.commit()
            
    
    def remove_role_permission(self, role_name: str, permission_name: str) -> None:
        """
        移除角色的权限
        
        Args:
            role_name: 角色名称
            permission_name: 权限名称
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM role_permissions WHERE role_name = ? AND permission_name = ?",
                          (role_name, permission_name))
            conn.commit()
            
    
    def get_role_permissions(self, role_name: str) -> Set[str]:
        """
        获取角色的所有权限（含继承）
        
        Args:
            role_name: 角色名称
        
        Returns:
            权限名称集合
        """
        permissions = set()
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 使用递归CTE获取角色及其所有父角色的权限
            cursor.execute("""
                WITH RECURSIVE role_hierarchy AS (
                    SELECT name, inherit_from FROM roles WHERE name = ?
                    UNION ALL
                    SELECT r.name, r.inherit_from 
                    FROM roles r 
                    JOIN role_hierarchy rh ON r.name = rh.inherit_from
                )
                SELECT DISTINCT rp.permission_name 
                FROM role_permissions rp
                JOIN role_hierarchy rh ON rp.role_name = rh.name
            """, (role_name,))
            
            for row in cursor.fetchall():
                permissions.add(row[0])
            
            
        
        return permissions
    
    def assign_role(self, user_id: str, role_name: str) -> None:
        """
        为用户分配角色
        
        Args:
            user_id: 用户ID
            role_name: 角色名称
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO user_roles (user_id, role_name) VALUES (?, ?)",
                          (user_id, role_name))
            conn.commit()
            
    
    def remove_role_from_user(self, user_id: str, role_name: str) -> None:
        """
        移除用户的角色
        
        Args:
            user_id: 用户ID
            role_name: 角色名称
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_roles WHERE user_id = ? AND role_name = ?",
                          (user_id, role_name))
            conn.commit()
            
    
    def get_user_roles(self, user_id: str) -> List[str]:
        """
        获取用户的所有角色
        
        Args:
            user_id: 用户ID
        
        Returns:
            角色名称列表
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT role_name FROM user_roles WHERE user_id = ?", (user_id,))
            roles = [row[0] for row in cursor.fetchall()]
            
            return roles
    
    def has_role(self, user_id: str, role_name: str) -> bool:
        """
        检查用户是否拥有指定角色
        
        Args:
            user_id: 用户ID
            role_name: 角色名称
        
        Returns:
            是否拥有该角色
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM user_roles WHERE user_id = ? AND role_name = ?",
                          (user_id, role_name))
            result = cursor.fetchone()
            
            return result is not None
    
    def has_permission(self, user_id: str, permission_name: str) -> bool:
        """
        检查用户是否拥有指定权限
        
        Args:
            user_id: 用户ID或角色名称
            permission_name: 权限名称
        
        Returns:
            是否拥有该权限
        """
        # 如果user_id是admin角色，拥有所有权限
        if user_id == "admin" or self.has_role(user_id, "admin"):
            return True
        
        # 获取用户的所有角色
        roles = self.get_user_roles(user_id)
        
        # 如果user_id本身是一个角色名，也加入检查
        if self._role_exists(user_id):
            roles.append(user_id)
        
        # 检查每个角色的权限（含继承）
        for role_name in roles:
            permissions = self.get_role_permissions(role_name)
            if permission_name in permissions:
                return True
        
        return False
    
    def _role_exists(self, role_name: str) -> bool:
        """
        检查角色是否存在
        
        Args:
            role_name: 角色名称
        
        Returns:
            角色是否存在
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM roles WHERE name = ?", (role_name,))
            result = cursor.fetchone()
            
            return result is not None
    
    def get_user_permissions(self, user_id: str) -> Set[str]:
        """
        获取用户的所有权限（含角色继承）
        
        Args:
            user_id: 用户ID
        
        Returns:
            权限名称集合
        """
        permissions = set()
        
        # admin角色拥有所有权限
        if self.has_role(user_id, "admin"):
            all_perms = self.list_permissions()
            return {perm.name for perm in all_perms}
        
        # 获取用户的所有角色
        roles = self.get_user_roles(user_id)
        
        # 收集所有角色的权限（含继承）
        for role_name in roles:
            role_perms = self.get_role_permissions(role_name)
            permissions.update(role_perms)
        
        return permissions


def require_permission(permission_name: str):
    """
    权限检查装饰器
    
    Args:
        permission_name: 所需权限名称
    
    Returns:
        装饰器函数
    """
    def decorator(func):
        def wrapper(user_id: str, *args, **kwargs):
            # 从参数或kwargs中获取rbac实例
            rbac = kwargs.get('rbac')
            if rbac is None:
                # 尝试从第一个参数之后获取
                if len(args) > 0 and isinstance(args[0], RBACManager):
                    rbac = args[0]
            
            if rbac is None:
                raise ValueError("RBACManager实例未提供")
            
            if not rbac.has_permission(user_id, permission_name):
                raise PermissionError(f"用户 {user_id} 没有权限: {permission_name}")
            
            return func(user_id, *args, **kwargs)
        return wrapper
    return decorator


def require_role(role_name: str):
    """
    角色检查装饰器
    
    Args:
        role_name: 所需角色名称
    
    Returns:
        装饰器函数
    """
    def decorator(func):
        def wrapper(user_id: str, *args, **kwargs):
            # 从参数或kwargs中获取rbac实例
            rbac = kwargs.get('rbac')
            if rbac is None:
                if len(args) > 0 and isinstance(args[0], RBACManager):
                    rbac = args[0]
            
            if rbac is None:
                raise ValueError("RBACManager实例未提供")
            
            if not rbac.has_role(user_id, role_name):
                raise PermissionError(f"用户 {user_id} 没有角色: {role_name}")
            
            return func(user_id, *args, **kwargs)
        return wrapper
    return decorator

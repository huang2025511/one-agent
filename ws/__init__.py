"""WebSocket 实时消息推送模块.

提供实时通知和事件推送功能，支持：
- 多连接管理
- 房间/频道订阅模式
- 广播消息
- 离线消息队列
- 心跳检测
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from jose import JWTError, jwt

from core.plugin import Plugin

logger = logging.getLogger(__name__)

# 消息类型定义
MESSAGE_TYPES = {
    "system_notification": "系统通知",
    "chat_message": "聊天消息",
    "task_status": "任务状态更新",
    "alert_triggered": "告警触发",
    "workflow_event": "工作流事件",
}


@dataclass
class WebSocketMessage:
    """WebSocket 消息数据模型."""
    message_id: str = field(default_factory=lambda: str(uuid4()))
    message_type: str = "system_notification"
    content: Dict[str, Any] = field(default_factory=dict)
    sender: Optional[str] = None
    room_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_json(self) -> str:
        """序列化为 JSON 字符串."""
        data = {
            "message_id": self.message_id,
            "message_type": self.message_type,
            "content": self.content,
            "sender": self.sender,
            "room_id": self.room_id,
            "timestamp": self.timestamp.isoformat(),
        }
        return json.dumps(data)

    @classmethod
    def from_json(cls, json_str: str) -> "WebSocketMessage":
        """从 JSON 字符串解析消息."""
        data = json.loads(json_str)
        return cls(
            message_id=data.get("message_id", str(uuid4())),
            message_type=data.get("message_type", "system_notification"),
            content=data.get("content", {}),
            sender=data.get("sender"),
            room_id=data.get("room_id"),
            timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now().isoformat())),
        )


@dataclass
class ConnectionInfo:
    """WebSocket 连接信息."""
    connection_id: str
    websocket: WebSocket
    user_id: Optional[str] = None
    username: Optional[str] = None
    subscribed_rooms: Set[str] = field(default_factory=set)
    last_heartbeat: datetime = field(default_factory=datetime.now)
    is_active: bool = True


class WebSocketManager(Plugin):
    """WebSocket 管理器，负责管理所有 WebSocket 连接和消息推送."""

    name: str = "websocket_manager"
    depends_on: List[str] = []
    load_priority: int = 10

    def __init__(self) -> None:
        super().__init__()
        self.connections: Dict[str, ConnectionInfo] = {}  # connection_id -> ConnectionInfo
        self.rooms: Dict[str, Set[str]] = {}  # room_id -> set of connection_ids
        self.offline_messages: Dict[str, List[WebSocketMessage]] = {}  # user_id -> messages
        self.lock = asyncio.Lock()
        self.heartbeat_interval = 30  # 心跳间隔（秒）
        self.connection_timeout = 60  # 连接超时时间（秒）
        self.max_offline_messages = 100  # 最大离线消息数

    async def setup(self, ctx: "AgentContext") -> None:
        """初始化 WebSocket 管理器."""
        await super().setup(ctx)
        logger.info("WebSocketManager setup completed")

    async def start(self) -> None:
        """启动 WebSocket 管理器，开始心跳检测任务."""
        await super().start()
        asyncio.create_task(self._heartbeat_monitor())

    async def stop(self) -> None:
        """停止 WebSocket 管理器，关闭所有连接."""
        await super().stop()
        async with self.lock:
            for conn_info in self.connections.values():
                try:
                    await conn_info.websocket.close()
                except Exception as exc:
                    logger.error("Failed to close connection %s: %s", conn_info.connection_id, exc)
            self.connections.clear()
            self.rooms.clear()

    async def connect(self, websocket: WebSocket, token: Optional[str] = None) -> str:
        """建立新的 WebSocket 连接.
        
        Args:
            websocket: FastAPI WebSocket 对象
            token: JWT 认证令牌
            
        Returns:
            连接 ID
        """
        connection_id = str(uuid4())
        user_id = None
        username = None

        # JWT 认证
        if token:
            try:
                # TODO: 从配置获取密钥和算法
                payload = jwt.decode(token, "secret", algorithms=["HS256"])
                user_id = payload.get("sub")
                username = payload.get("username")
            except JWTError as exc:
                logger.warning("Invalid JWT token: %s", exc)

        async with self.lock:
            self.connections[connection_id] = ConnectionInfo(
                connection_id=connection_id,
                websocket=websocket,
                user_id=user_id,
                username=username,
            )

        logger.info("New connection: %s (user: %s)", connection_id, user_id)

        # 发送离线消息
        if user_id and user_id in self.offline_messages:
            await self._send_offline_messages(connection_id, user_id)

        return connection_id

    async def disconnect(self, connection_id: str) -> None:
        """断开 WebSocket 连接."""
        async with self.lock:
            if connection_id in self.connections:
                conn_info = self.connections.pop(connection_id)

                # 从所有订阅的房间中移除
                for room_id in conn_info.subscribed_rooms:
                    if room_id in self.rooms:
                        self.rooms[room_id].discard(connection_id)
                        if not self.rooms[room_id]:
                            del self.rooms[room_id]

                logger.info("Connection closed: %s", connection_id)

    async def subscribe(self, connection_id: str, room_id: str) -> None:
        """订阅指定房间."""
        async with self.lock:
            if connection_id in self.connections:
                conn_info = self.connections[connection_id]
                conn_info.subscribed_rooms.add(room_id)

                if room_id not in self.rooms:
                    self.rooms[room_id] = set()
                self.rooms[room_id].add(connection_id)

                logger.debug("Connection %s subscribed to room %s", connection_id, room_id)

    async def unsubscribe(self, connection_id: str, room_id: str) -> None:
        """取消订阅指定房间."""
        async with self.lock:
            if connection_id in self.connections:
                conn_info = self.connections[connection_id]
                conn_info.subscribed_rooms.discard(room_id)

                if room_id in self.rooms:
                    self.rooms[room_id].discard(connection_id)
                    if not self.rooms[room_id]:
                        del self.rooms[room_id]

                logger.debug("Connection %s unsubscribed from room %s", connection_id, room_id)

    async def broadcast(self, message: WebSocketMessage) -> None:
        """广播消息到所有连接."""
        async with self.lock:
            for conn_info in self.connections.values():
                if conn_info.is_active:
                    asyncio.create_task(self._send_message(conn_info.connection_id, message))

    async def broadcast_to_room(self, room_id: str, message: WebSocketMessage) -> None:
        """广播消息到指定房间."""
        message.room_id = room_id

        async with self.lock:
            if room_id in self.rooms:
                for connection_id in self.rooms[room_id]:
                    if connection_id in self.connections and self.connections[connection_id].is_active:
                        asyncio.create_task(self._send_message(connection_id, message))

    async def send_to_user(self, user_id: str, message: WebSocketMessage) -> None:
        """发送消息给指定用户.
        
        如果用户离线，消息将被暂存到离线消息队列.
        """
        async with self.lock:
            online_connections = [
                conn_info for conn_info in self.connections.values()
                if conn_info.user_id == user_id and conn_info.is_active
            ]

            if online_connections:
                for conn_info in online_connections:
                    asyncio.create_task(self._send_message(conn_info.connection_id, message))
            else:
                # 离线消息暂存
                if user_id not in self.offline_messages:
                    self.offline_messages[user_id] = []
                self.offline_messages[user_id].append(message)

                # 限制离线消息数量
                if len(self.offline_messages[user_id]) > self.max_offline_messages:
                    self.offline_messages[user_id] = self.offline_messages[user_id][-self.max_offline_messages:]

                logger.debug("Stored offline message for user %s", user_id)

    async def send_to_connection(self, connection_id: str, message: WebSocketMessage) -> None:
        """发送消息给指定连接."""
        async with self.lock:
            if connection_id in self.connections and self.connections[connection_id].is_active:
                asyncio.create_task(self._send_message(connection_id, message))

    async def update_heartbeat(self, connection_id: str) -> None:
        """更新连接心跳时间."""
        async with self.lock:
            if connection_id in self.connections:
                self.connections[connection_id].last_heartbeat = datetime.now()

    async def _send_message(self, connection_id: str, message: WebSocketMessage) -> None:
        """向指定连接发送消息."""
        async with self.lock:
            if connection_id not in self.connections:
                return
            conn_info = self.connections[connection_id]

        try:
            await conn_info.websocket.send_text(message.to_json())
            logger.debug("Message sent to connection %s", connection_id)
        except WebSocketDisconnect:
            logger.debug("WebSocket disconnected: %s", connection_id)
            await self.disconnect(connection_id)
        except Exception as exc:
            logger.error("Failed to send message to connection %s: %s", connection_id, exc)

    async def _send_offline_messages(self, connection_id: str, user_id: str) -> None:
        """发送离线消息给用户."""
        async with self.lock:
            if user_id not in self.offline_messages:
                return

            messages = self.offline_messages[user_id]
            conn_info = self.connections.get(connection_id)
            if not conn_info or not conn_info.is_active:
                return

        for message in messages:
            await self._send_message(connection_id, message)

        # 清空已发送的离线消息
        async with self.lock:
            if user_id in self.offline_messages:
                del self.offline_messages[user_id]

        logger.info("Sent %d offline messages to user %s", len(messages), user_id)

    async def _heartbeat_monitor(self) -> None:
        """心跳监控任务，定期检查连接状态."""
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)

                async with self.lock:
                    now = datetime.now()
                    to_disconnect = []

                    for conn_id, conn_info in self.connections.items():
                        time_diff = (now - conn_info.last_heartbeat).total_seconds()
                        if time_diff > self.connection_timeout:
                            to_disconnect.append(conn_id)
                            conn_info.is_active = False

                    for conn_id in to_disconnect:
                        logger.warning("Connection timeout: %s", conn_id)
                        del self.connections[conn_id]

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Heartbeat monitor error: %s", exc)

    def get_connection_count(self) -> int:
        """获取当前连接数量."""
        return len(self.connections)

    def get_room_count(self) -> int:
        """获取当前房间数量."""
        return len(self.rooms)

    def get_user_connections(self, user_id: str) -> List[ConnectionInfo]:
        """获取指定用户的所有连接."""
        return [
            conn_info for conn_info in self.connections.values()
            if conn_info.user_id == user_id
        ]

"""3D 空间交互模块 — 场景管理、空间对话、场景理解、手势/语音交互与 AR/VR 适配。

提供：
  - 场景管理（SceneManager）：场景创建/编辑/删除，物体管理，相机与光照设置
  - 空间对话（SpatialDialogue）：3D 空间中的 AI avatar、空间定位消息、上下文感知
  - 场景理解（SceneUnderstanding）：物体分类、空间关系分析、场景描述生成
  - 手势识别（GestureRecognizer）：点击/拖拽/缩放/旋转/捏合等手势定义与事件分发
  - 语音交互（VoiceInteraction）：语音命令解析、空间语音定位（3D 音效）、多语言支持
  - AR/VR 适配（ARVRAdapter）：设备检测、渲染模式切换、性能自适应
  - Spatial3DPlugin：整合以上能力的插件入口

输出为纯 Python 生成的 Three.js 兼容 JSON 场景数据，前端可直接加载渲染。
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# ============================================================
# 基础数据结构
# ============================================================

@dataclass
class Vector3:
    """三维向量。"""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def to_list(self) -> List[float]:
        """转为列表 [x, y, z]。"""
        return [self.x, self.y, self.z]

    @classmethod
    def from_list(cls, data: Any) -> "Vector3":
        """从列表构造。"""
        return cls(x=float(data[0]), y=float(data[1]), z=float(data[2]))

    def add(self, other: "Vector3") -> "Vector3":
        """向量加法。"""
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)

    def sub(self, other: "Vector3") -> "Vector3":
        """向量减法。"""
        return Vector3(self.x - other.x, self.y - other.y, self.z - other.z)

    def distance_to(self, other: "Vector3") -> float:
        """计算到另一个向量的欧氏距离。"""
        dx = self.x - other.x
        dy = self.y - other.y
        dz = self.z - other.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)


@dataclass
class Quaternion:
    """四元数（表示旋转，w 为标量分量）。"""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 1.0

    def to_list(self) -> List[float]:
        """转为列表 [x, y, z, w]。"""
        return [self.x, self.y, self.z, self.w]

    @classmethod
    def from_euler(cls, rx: float, ry: float, rz: float) -> "Quaternion":
        """从欧拉角（弧度）构造四元数。"""
        cx, sx = math.cos(rx / 2), math.sin(rx / 2)
        cy, sy = math.cos(ry / 2), math.sin(ry / 2)
        cz, sz = math.cos(rz / 2), math.sin(rz / 2)
        return cls(
            x=sx * cy * cz + cx * sy * sz,
            y=cx * sy * cz - sx * cy * sz,
            z=cx * cy * sz + sx * sy * cz,
            w=cx * cy * cz - sx * sy * sz,
        )


@dataclass
class Transform:
    """变换：位置、旋转、缩放。"""

    position: Vector3 = field(default_factory=Vector3)
    rotation: Quaternion = field(default_factory=Quaternion)
    scale: Vector3 = field(default_factory=lambda: Vector3(1.0, 1.0, 1.0))

    def to_matrix(self) -> List[float]:
        """生成 4x4 变换矩阵（列主序，Three.js 兼容，共 16 个元素）。"""
        p = self.position
        q = self.rotation
        s = self.scale
        x, y, z, w = q.x, q.y, q.z, q.w
        xx, yy, zz = x * x, y * y, z * z
        xy, xz, yz = x * y, x * z, y * z
        wx, wy, wz = w * x, w * y, w * z
        # 旋转矩阵分量
        r00 = 1 - 2 * (yy + zz)
        r01 = 2 * (xy - wz)
        r02 = 2 * (xz + wy)
        r10 = 2 * (xy + wz)
        r11 = 1 - 2 * (xx + zz)
        r12 = 2 * (yz - wx)
        r20 = 2 * (xz - wy)
        r21 = 2 * (yz + wx)
        r22 = 1 - 2 * (xx + yy)
        # 列主序：每 4 个元素为一列
        return [
            r00 * s.x, r10 * s.x, r20 * s.x, 0.0,
            r01 * s.y, r11 * s.y, r21 * s.y, 0.0,
            r02 * s.z, r12 * s.z, r22 * s.z, 0.0,
            p.x, p.y, p.z, 1.0,
        ]

    def translate(self, dx: float, dy: float, dz: float) -> "Transform":
        """平移并返回自身（链式调用）。"""
        self.position = self.position.add(Vector3(dx, dy, dz))
        return self

    def rotate_euler(self, rx: float, ry: float, rz: float) -> "Transform":
        """以欧拉角（弧度）旋转并返回自身。"""
        self.rotation = Quaternion.from_euler(rx, ry, rz)
        return self

    def scale_by(self, sx: float, sy: float, sz: float) -> "Transform":
        """缩放并返回自身。"""
        self.scale = Vector3(sx, sy, sz)
        return self


# ============================================================
# 场景数据结构
# ============================================================

class ObjectType(str, Enum):
    """物体类型枚举。"""

    MESH = "Mesh"
    GROUP = "Group"
    LIGHT = "Light"
    CAMERA = "Camera"
    AVATAR = "Avatar"
    TEXT = "Text"
    PARTICLES = "Particles"


class GeometryType(str, Enum):
    """几何体类型枚举。"""

    BOX = "BoxGeometry"
    SPHERE = "SphereGeometry"
    CYLINDER = "CylinderGeometry"
    PLANE = "PlaneGeometry"
    CONE = "ConeGeometry"
    TORUS = "TorusGeometry"


class LightType(str, Enum):
    """光源类型枚举。"""

    AMBIENT = "AmbientLight"
    DIRECTIONAL = "DirectionalLight"
    POINT = "PointLight"
    SPOT = "SpotLight"
    HEMISPHERE = "HemisphereLight"


class CameraType(str, Enum):
    """相机类型枚举。"""

    PERSPECTIVE = "PerspectiveCamera"
    ORTHOGRAPHIC = "OrthographicCamera"


@dataclass
class Geometry:
    """几何体定义。"""

    type: GeometryType = GeometryType.BOX
    # 通用参数字典，例如 BoxGeometry: {"width":1,"height":1,"depth":1}
    parameters: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转为 Three.js 几何体描述。"""
        return {
            "type": self.type.value,
            "parameters": dict(self.parameters),
        }


@dataclass
class Material:
    """材质定义。"""

    type: str = "MeshStandardMaterial"
    color: int = 0xFFFFFF
    roughness: float = 0.5
    metalness: float = 0.0
    opacity: float = 1.0
    transparent: bool = False
    emissive: int = 0x000000
    # 额外参数（贴图等）
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转为 Three.js 材质描述。"""
        data: Dict[str, Any] = {
            "type": self.type,
            "color": self.color,
            "roughness": self.roughness,
            "metalness": self.metalness,
            "opacity": self.opacity,
            "transparent": self.transparent,
            "emissive": self.emissive,
        }
        data.update(self.properties)
        return data


@dataclass
class SceneObject:
    """场景物体。"""

    object_id: str
    name: str
    object_type: ObjectType = ObjectType.MESH
    transform: Transform = field(default_factory=Transform)
    geometry: Optional[Geometry] = None
    material: Optional[Material] = None
    visible: bool = True
    # 子物体 ID 列表（用于 Group 层级）
    children: List[str] = field(default_factory=list)
    # 自定义元数据（分类标签、物理属性等）
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """转为 Three.js 物体描述（不含子物体嵌套）。"""
        data: Dict[str, Any] = {
            "type": self.object_type.value,
            "uuid": self.object_id,
            "name": self.name,
            "visible": self.visible,
            "matrix": self.transform.to_matrix(),
            "metadata": dict(self.metadata),
        }
        if self.geometry is not None:
            data["geometry"] = self.geometry.to_dict()
        if self.material is not None:
            data["material"] = self.material.to_dict()
        return data


@dataclass
class Camera:
    """相机定义。"""

    camera_id: str
    name: str
    camera_type: CameraType = CameraType.PERSPECTIVE
    transform: Transform = field(default_factory=Transform)
    fov: float = 60.0
    aspect: float = 1.7778
    near: float = 0.1
    far: float = 1000.0
    # 正交相机参数
    ortho_left: float = -10.0
    ortho_right: float = 10.0
    ortho_top: float = 10.0
    ortho_bottom: float = -10.0
    active: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """转为 Three.js 相机描述。"""
        data: Dict[str, Any] = {
            "type": self.camera_type.value,
            "uuid": self.camera_id,
            "name": self.name,
            "matrix": self.transform.to_matrix(),
        }
        if self.camera_type == CameraType.PERSPECTIVE:
            data["fov"] = self.fov
            data["aspect"] = self.aspect
            data["near"] = self.near
            data["far"] = self.far
        else:
            data["left"] = self.ortho_left
            data["right"] = self.ortho_right
            data["top"] = self.ortho_top
            data["bottom"] = self.ortho_bottom
            data["near"] = self.near
            data["far"] = self.far
        return data


@dataclass
class Light:
    """光源定义。"""

    light_id: str
    name: str
    light_type: LightType = LightType.DIRECTIONAL
    transform: Transform = field(default_factory=Transform)
    color: int = 0xFFFFFF
    intensity: float = 1.0
    # 点光源/聚光灯衰减距离
    distance: float = 0.0
    decay: float = 2.0
    # 半球光地面颜色
    ground_color: int = 0x000000
    # 聚光灯参数
    angle: float = math.pi / 4
    penumbra: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """转为 Three.js 光源描述。"""
        data: Dict[str, Any] = {
            "type": self.light_type.value,
            "uuid": self.light_id,
            "name": self.name,
            "color": self.color,
            "intensity": self.intensity,
            "matrix": self.transform.to_matrix(),
        }
        if self.light_type in (LightType.POINT, LightType.SPOT):
            data["distance"] = self.distance
            data["decay"] = self.decay
        if self.light_type == LightType.HEMISPHERE:
            data["groundColor"] = self.ground_color
        if self.light_type == LightType.SPOT:
            data["angle"] = self.angle
            data["penumbra"] = self.penumbra
        return data


@dataclass
class Scene:
    """3D 场景。"""

    scene_id: str
    name: str
    description: str = ""
    objects: Dict[str, SceneObject] = field(default_factory=dict)
    cameras: Dict[str, Camera] = field(default_factory=dict)
    lights: Dict[str, Light] = field(default_factory=dict)
    active_camera_id: Optional[str] = None
    # 场景级背景与雾
    background: int = 0x1a1a2e
    fog: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        """更新修改时间。"""
        self.updated_at = time.time()


# ============================================================
# 场景管理器
# ============================================================

class SceneManager:
    """场景管理器 — 场景的创建/编辑/删除，物体、相机与光照管理。

    所有场景数据可导出为 Three.js 兼容的 JSON，前端 ``THREE.ObjectLoader``
    可直接加载。
    """

    def __init__(self) -> None:
        self._scenes: Dict[str, Scene] = {}

    # --------------------------------------------------- 场景生命周期
    def create_scene(
        self,
        scene_id: str,
        name: str,
        description: str = "",
    ) -> Scene:
        """创建新场景。若 ID 已存在则返回已有场景。"""
        if scene_id in self._scenes:
            logger.warning("场景 %s 已存在，返回已有场景", scene_id)
            return self._scenes[scene_id]
        scene = Scene(scene_id=scene_id, name=name, description=description)
        self._scenes[scene_id] = scene
        logger.info("创建场景: %s (%s)", scene_id, name)
        return scene

    def get_scene(self, scene_id: str) -> Optional[Scene]:
        """获取场景。"""
        return self._scenes.get(scene_id)

    def list_scenes(self) -> List[Dict[str, Any]]:
        """列出所有场景概要。"""
        result: List[Dict[str, Any]] = []
        for sid, scene in self._scenes.items():
            result.append({
                "scene_id": sid,
                "name": scene.name,
                "description": scene.description,
                "object_count": len(scene.objects),
                "camera_count": len(scene.cameras),
                "light_count": len(scene.lights),
                "updated_at": scene.updated_at,
            })
        return result

    def edit_scene(
        self,
        scene_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        background: Optional[int] = None,
        fog: Optional[Dict[str, Any]] = None,
    ) -> Optional[Scene]:
        """编辑场景元信息。"""
        scene = self._scenes.get(scene_id)
        if scene is None:
            logger.warning("编辑失败：场景 %s 不存在", scene_id)
            return None
        if name is not None:
            scene.name = name
        if description is not None:
            scene.description = description
        if background is not None:
            scene.background = background
        if fog is not None:
            scene.fog = dict(fog)
        scene.touch()
        logger.info("已编辑场景: %s", scene_id)
        return scene

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景。"""
        if scene_id not in self._scenes:
            return False
        del self._scenes[scene_id]
        logger.info("已删除场景: %s", scene_id)
        return True

    # --------------------------------------------------- 物体管理
    def add_object(
        self,
        scene_id: str,
        name: str,
        object_type: ObjectType = ObjectType.MESH,
        geometry: Optional[Geometry] = None,
        material: Optional[Material] = None,
        transform: Optional[Transform] = None,
        metadata: Optional[Dict[str, Any]] = None,
        object_id: Optional[str] = None,
    ) -> Optional[SceneObject]:
        """向场景添加物体。"""
        scene = self._scenes.get(scene_id)
        if scene is None:
            logger.warning("添加物体失败：场景 %s 不存在", scene_id)
            return None
        oid = object_id or f"obj_{uuid.uuid4().hex[:12]}"
        obj = SceneObject(
            object_id=oid,
            name=name,
            object_type=object_type,
            transform=transform or Transform(),
            geometry=geometry,
            material=material,
            metadata=metadata or {},
        )
        scene.objects[oid] = obj
        scene.touch()
        logger.info("已向场景 %s 添加物体 %s (%s)", scene_id, oid, name)
        return obj

    def remove_object(self, scene_id: str, object_id: str) -> bool:
        """从场景移除物体。"""
        scene = self._scenes.get(scene_id)
        if scene is None or object_id not in scene.objects:
            return False
        del scene.objects[object_id]
        # 从父级 Group 的 children 中清理
        for obj in scene.objects.values():
            if object_id in obj.children:
                obj.children = [c for c in obj.children if c != object_id]
        scene.touch()
        logger.info("已从场景 %s 移除物体 %s", scene_id, object_id)
        return True

    def get_object(self, scene_id: str, object_id: str) -> Optional[SceneObject]:
        """获取场景中的物体。"""
        scene = self._scenes.get(scene_id)
        if scene is None:
            return None
        return scene.objects.get(object_id)

    def list_objects(self, scene_id: str) -> List[SceneObject]:
        """列出场景中所有物体。"""
        scene = self._scenes.get(scene_id)
        if scene is None:
            return []
        return list(scene.objects.values())

    def transform_object(
        self,
        scene_id: str,
        object_id: str,
        position: Optional[Vector3] = None,
        rotation: Optional[Quaternion] = None,
        scale: Optional[Vector3] = None,
    ) -> Optional[SceneObject]:
        """变换物体（设置位置/旋转/缩放）。"""
        obj = self.get_object(scene_id, object_id)
        if obj is None:
            logger.warning("变换失败：物体 %s 不存在", object_id)
            return None
        if position is not None:
            obj.transform.position = position
        if rotation is not None:
            obj.transform.rotation = rotation
        if scale is not None:
            obj.transform.scale = scale
        self._scenes[scene_id].touch()
        return obj

    def add_child(self, scene_id: str, parent_id: str, child_id: str) -> bool:
        """建立父子层级关系（Group）。"""
        scene = self._scenes.get(scene_id)
        if scene is None:
            return False
        parent = scene.objects.get(parent_id)
        child = scene.objects.get(child_id)
        if parent is None or child is None:
            return False
        if child_id not in parent.children:
            parent.children.append(child_id)
        scene.touch()
        return True

    # --------------------------------------------------- 相机管理
    def add_camera(
        self,
        scene_id: str,
        name: str,
        camera_type: CameraType = CameraType.PERSPECTIVE,
        transform: Optional[Transform] = None,
        fov: float = 60.0,
        active: bool = False,
        camera_id: Optional[str] = None,
    ) -> Optional[Camera]:
        """添加相机。"""
        scene = self._scenes.get(scene_id)
        if scene is None:
            return None
        cid = camera_id or f"cam_{uuid.uuid4().hex[:12]}"
        camera = Camera(
            camera_id=cid,
            name=name,
            camera_type=camera_type,
            transform=transform or Transform(),
            fov=fov,
            active=active,
        )
        scene.cameras[cid] = camera
        if active:
            self.set_active_camera(scene_id, cid)
        scene.touch()
        logger.info("已向场景 %s 添加相机 %s", scene_id, cid)
        return camera

    def set_active_camera(self, scene_id: str, camera_id: str) -> bool:
        """设置当前激活相机。"""
        scene = self._scenes.get(scene_id)
        if scene is None or camera_id not in scene.cameras:
            return False
        for cam in scene.cameras.values():
            cam.active = False
        scene.cameras[camera_id].active = True
        scene.active_camera_id = camera_id
        scene.touch()
        logger.info("场景 %s 激活相机: %s", scene_id, camera_id)
        return True

    def get_active_camera(self, scene_id: str) -> Optional[Camera]:
        """获取当前激活相机。"""
        scene = self._scenes.get(scene_id)
        if scene is None or scene.active_camera_id is None:
            return None
        return scene.cameras.get(scene.active_camera_id)

    # --------------------------------------------------- 光照管理
    def add_light(
        self,
        scene_id: str,
        name: str,
        light_type: LightType = LightType.DIRECTIONAL,
        transform: Optional[Transform] = None,
        color: int = 0xFFFFFF,
        intensity: float = 1.0,
        light_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[Light]:
        """添加光源。"""
        scene = self._scenes.get(scene_id)
        if scene is None:
            return None
        lid = light_id or f"lit_{uuid.uuid4().hex[:12]}"
        light = Light(
            light_id=lid,
            name=name,
            light_type=light_type,
            transform=transform or Transform(),
            color=color,
            intensity=intensity,
        )
        # 应用额外参数
        for key, value in kwargs.items():
            if hasattr(light, key):
                setattr(light, key, value)
        scene.lights[lid] = light
        scene.touch()
        logger.info("已向场景 %s 添加光源 %s (%s)", scene_id, lid, light_type.value)
        return light

    def remove_light(self, scene_id: str, light_id: str) -> bool:
        """移除光源。"""
        scene = self._scenes.get(scene_id)
        if scene is None or light_id not in scene.lights:
            return False
        del scene.lights[light_id]
        scene.touch()
        return True

    # --------------------------------------------------- Three.js JSON 导出
    def export_scene_json(self, scene_id: str) -> Optional[Dict[str, Any]]:
        """导出为 Three.js 兼容的 JSON 场景数据。

        生成的结构可被 ``THREE.ObjectLoader.parse`` 直接加载。
        """
        scene = self._scenes.get(scene_id)
        if scene is None:
            logger.warning("导出失败：场景 %s 不存在", scene_id)
            return None

        geometries: List[Dict[str, Any]] = []
        materials: List[Dict[str, Any]] = []
        children: List[Dict[str, Any]] = []

        # 物体 → 子节点
        for obj in scene.objects.values():
            if obj.geometry is not None:
                geometries.append(obj.geometry.to_dict())
            if obj.material is not None:
                materials.append(obj.material.to_dict())
            children.append(self._object_to_three_node(obj, scene))

        # 光源 → 子节点
        for light in scene.lights.values():
            children.append(light.to_dict())

        scene_node: Dict[str, Any] = {
            "type": "Scene",
            "uuid": scene.scene_id,
            "name": scene.name,
            "background": scene.background,
            "children": children,
        }
        if scene.fog is not None:
            scene_node["fog"] = dict(scene.fog)

        # 相机单独列出（Three.js 场景 JSON 顶层可含 cameras）
        cameras_json: List[Dict[str, Any]] = []
        active_camera_uuid = None
        for cam in scene.cameras.values():
            cameras_json.append(cam.to_dict())
            if cam.active:
                active_camera_uuid = cam.camera_id

        result: Dict[str, Any] = {
            "metadata": {
                "version": 4.5,
                "type": "Object",
                "generator": "Spatial3D-SceneManager",
            },
            "geometries": geometries,
            "materials": materials,
            "object": scene_node,
        }
        if cameras_json:
            result["cameras"] = cameras_json
        if active_camera_uuid is not None:
            result["activeCamera"] = active_camera_uuid
        logger.info("已导出场景 %s 的 Three.js JSON（物体 %d，光源 %d，相机 %d）",
                    scene_id, len(scene.objects), len(scene.lights), len(scene.cameras))
        return result

    def export_scene_string(self, scene_id: str, indent: int = 2) -> str:
        """导出场景 JSON 字符串。"""
        data = self.export_scene_json(scene_id)
        if data is None:
            return "{}"
        return json.dumps(data, ensure_ascii=False, indent=indent)

    def _object_to_three_node(self, obj: SceneObject, scene: Scene) -> Dict[str, Any]:
        """将物体转为 Three.js 节点（含子物体嵌套）。"""
        node = obj.to_dict()
        # 嵌套子物体
        child_nodes: List[Dict[str, Any]] = []
        for child_id in obj.children:
            child = scene.objects.get(child_id)
            if child is not None:
                child_nodes.append(self._object_to_three_node(child, scene))
        if child_nodes:
            node["children"] = child_nodes
        return node


# ============================================================
# 空间对话
# ============================================================

@dataclass
class Avatar:
    """AI 助手 avatar（3D 空间中的具象化形象）。"""

    avatar_id: str
    name: str
    transform: Transform = field(default_factory=Transform)
    # avatar 外观模型（可指向场景中的物体 ID）
    model_object_id: Optional[str] = None
    # 当前情绪/状态
    mood: str = "neutral"
    speaking: bool = False
    # 可见范围（用户进入此距离触发对话）
    interaction_radius: float = 5.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SpatialMessage:
    """空间定位消息 — 锚定在 3D 空间中的某一点。"""

    message_id: str
    role: str  # "user" | "assistant" | "system"
    content: str
    # 消息锚定的空间位置
    position: Vector3 = field(default_factory=Vector3)
    # 关联的 avatar（可选）
    avatar_id: Optional[str] = None
    # 关联的场景物体（可选，用于上下文感知）
    target_object_id: Optional[str] = None
    language: str = "zh"
    timestamp: float = field(default_factory=time.time)


class SpatialDialogue:
    """空间对话 — 在 3D 空间中放置 AI avatar 并进行位置感知的对话。

    消息可锚定到空间坐标，使对话具备空间上下文：当用户靠近某个物体时，
    对话可自动关联该物体作为上下文。
    """

    def __init__(self) -> None:
        self._avatars: Dict[str, Avatar] = {}
        self._messages: List[SpatialMessage] = []
        # 当前用户位置（用于上下文感知）
        self._user_position: Vector3 = Vector3()

    # --------------------------------------------------- avatar 管理
    def place_avatar(
        self,
        name: str,
        position: Vector3,
        model_object_id: Optional[str] = None,
        mood: str = "neutral",
        interaction_radius: float = 5.0,
        avatar_id: Optional[str] = None,
    ) -> Avatar:
        """在 3D 空间中放置一个 AI avatar。"""
        aid = avatar_id or f"avatar_{uuid.uuid4().hex[:12]}"
        avatar = Avatar(
            avatar_id=aid,
            name=name,
            transform=Transform(position=position),
            model_object_id=model_object_id,
            mood=mood,
            interaction_radius=interaction_radius,
        )
        self._avatars[aid] = avatar
        logger.info("已放置 avatar %s 于 %s", name, position.to_list())
        return avatar

    def get_avatar(self, avatar_id: str) -> Optional[Avatar]:
        """获取 avatar。"""
        return self._avatars.get(avatar_id)

    def list_avatars(self) -> List[Avatar]:
        """列出所有 avatar。"""
        return list(self._avatars.values())

    def remove_avatar(self, avatar_id: str) -> bool:
        """移除 avatar。"""
        if avatar_id not in self._avatars:
            return False
        del self._avatars[avatar_id]
        return True

    def set_avatar_mood(self, avatar_id: str, mood: str) -> Optional[Avatar]:
        """设置 avatar 情绪状态。"""
        avatar = self._avatars.get(avatar_id)
        if avatar is None:
            return None
        avatar.mood = mood
        return avatar

    def find_nearest_avatar(self, position: Optional[Vector3] = None) -> Optional[Avatar]:
        """查找距离给定位置（默认用户位置）最近的 avatar。"""
        pos = position or self._user_position
        if not self._avatars:
            return None
        return min(
            self._avatars.values(),
            key=lambda a: a.transform.position.distance_to(pos),
        )

    # --------------------------------------------------- 空间消息
    def send_message(
        self,
        role: str,
        content: str,
        position: Optional[Vector3] = None,
        avatar_id: Optional[str] = None,
        target_object_id: Optional[str] = None,
        language: str = "zh",
    ) -> SpatialMessage:
        """发送一条空间定位消息。

        若未指定位置，则使用当前用户位置；若指定了 avatar，则锚定到 avatar 位置。
        """
        if avatar_id is not None:
            avatar = self._avatars.get(avatar_id)
            if avatar is not None:
                position = position or avatar.transform.position
        pos = position or self._user_position
        msg = SpatialMessage(
            message_id=f"msg_{uuid.uuid4().hex[:12]}",
            role=role,
            content=content,
            position=pos,
            avatar_id=avatar_id,
            target_object_id=target_object_id,
            language=language,
        )
        self._messages.append(msg)
        # 若是 assistant 消息，标记 avatar 为正在说话
        if role == "assistant" and avatar_id is not None:
            avatar = self._avatars.get(avatar_id)
            if avatar is not None:
                avatar.speaking = True
        logger.info("空间消息[%s] @%s: %s", role, pos.to_list(), content[:50])
        return msg

    def get_messages(
        self,
        avatar_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[SpatialMessage]:
        """获取消息历史（可按 avatar 过滤）。"""
        if avatar_id is not None:
            msgs = [m for m in self._messages if m.avatar_id == avatar_id]
        else:
            msgs = list(self._messages)
        return msgs[-limit:]

    def clear_messages(self) -> int:
        """清空消息历史，返回清除数量。"""
        count = len(self._messages)
        self._messages.clear()
        # 重置所有 avatar 说话状态
        for avatar in self._avatars.values():
            avatar.speaking = False
        return count

    # --------------------------------------------------- 上下文感知
    def update_user_position(self, position: Vector3) -> None:
        """更新用户空间位置（用于上下文感知）。"""
        self._user_position = position

    def get_user_position(self) -> Vector3:
        """获取当前用户位置。"""
        return self._user_position

    def get_spatial_context(
        self,
        scene: Optional[Scene] = None,
        radius: float = 10.0,
    ) -> Dict[str, Any]:
        """基于用户当前位置生成空间上下文。

        返回附近的 avatar、附近物体以及最近 avatar，供对话系统增强上下文。
        """
        pos = self._user_position
        nearby_avatars = [
            {
                "avatar_id": a.avatar_id,
                "name": a.name,
                "distance": round(a.transform.position.distance_to(pos), 3),
                "mood": a.mood,
            }
            for a in self._avatars.values()
            if a.transform.position.distance_to(pos) <= a.interaction_radius
        ]
        nearby_avatars.sort(key=lambda x: x["distance"])

        nearby_objects: List[Dict[str, Any]] = []
        if scene is not None:
            for obj in scene.objects.values():
                dist = obj.transform.position.distance_to(pos)
                if dist <= radius:
                    nearby_objects.append({
                        "object_id": obj.object_id,
                        "name": obj.name,
                        "type": obj.object_type.value,
                        "distance": round(dist, 3),
                    })
            nearby_objects.sort(key=lambda x: x["distance"])

        nearest = self.find_nearest_avatar()
        return {
            "user_position": pos.to_list(),
            "nearby_avatars": nearby_avatars,
            "nearby_objects": nearby_objects,
            "nearest_avatar_id": nearest.avatar_id if nearest else None,
            "radius": radius,
        }


# ============================================================
# 场景理解
# ============================================================

# 物体分类标签 → 默认几何体/材质映射
_CATEGORY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "furniture": {"geometry": GeometryType.BOX, "color": 0x8B4513},
    "electronics": {"geometry": GeometryType.BOX, "color": 0x333333},
    "plant": {"geometry": GeometryType.SPHERE, "color": 0x2E8B57},
    "vehicle": {"geometry": GeometryType.BOX, "color": 0xC0C0C0},
    "character": {"geometry": GeometryType.CYLINDER, "color": 0xFFD700},
    "building": {"geometry": GeometryType.BOX, "color": 0xA9A9A9},
    "tool": {"geometry": GeometryType.CYLINDER, "color": 0x4682B4},
    "decoration": {"geometry": GeometryType.SPHERE, "color": 0xFF69B4},
}


class SceneUnderstanding:
    """场景理解 — 物体分类、空间关系分析与场景描述生成。

    基于场景中物体的位置与元数据，推断物体间的空间关系（前后/左右/上下）
    并生成自然语言场景描述。
    """

    # 空间关系阈值（单位与场景坐标一致）
    RELATION_THRESHOLD: float = 0.5

    def classify_object(self, obj: SceneObject) -> str:
        """对物体进行分类。

        优先使用物体元数据中的 ``category``；其次根据名称关键词推断；
        最后返回 "unknown"。
        """
        # 1. 元数据显式分类
        category = obj.metadata.get("category")
        if category and isinstance(category, str):
            return category
        # 2. 名称关键词推断
        name_lower = obj.name.lower()
        keyword_map = {
            "furniture": ["桌", "椅", "床", "柜", "table", "chair", "bed", "shelf"],
            "electronics": ["电脑", "电视", "屏幕", "phone", "screen", "monitor", "computer"],
            "plant": ["树", "花", "草", "plant", "tree", "flower"],
            "vehicle": ["车", "船", "car", "vehicle", "bike"],
            "character": ["人", "角色", "avatar", "person", "character", "robot"],
            "building": ["楼", "房", "building", "house", "tower"],
            "tool": ["工具", "tool", "hammer", "wrench"],
            "decoration": ["装饰", "decoration", "ornament", "painting"],
        }
        for cat, keywords in keyword_map.items():
            if any(kw in name_lower for kw in keywords):
                return cat
        return "unknown"

    def get_category_defaults(self, category: str) -> Dict[str, Any]:
        """获取分类对应的默认几何体/颜色。"""
        return _CATEGORY_DEFAULTS.get(category, {
            "geometry": GeometryType.BOX,
            "color": 0xCCCCCC,
        })

    def analyze_spatial_relations(
        self,
        scene: Scene,
        reference_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """分析场景中物体间的空间关系。

        以 ``reference_id`` 指定的物体（或第一个物体）为参考，计算其它物体
        相对参考物的"前/后/左/右/上/下"关系。

        约定（Three.js 坐标系，相机朝 -Z）：
          - 前/后：Z 轴（-Z 为前）
          - 左/右：X 轴（+X 为右）
          - 上/下：Y 轴（+Y 为上）
        """
        objects = list(scene.objects.values())
        if not objects:
            return []
        # 确定参考物体
        ref = None
        if reference_id is not None:
            ref = scene.objects.get(reference_id)
        if ref is None:
            ref = objects[0]
        ref_pos = ref.transform.position

        relations: List[Dict[str, Any]] = []
        for obj in objects:
            if obj.object_id == ref.object_id:
                continue
            delta = obj.transform.position.sub(ref_pos)
            rel = self._compute_relation(delta)
            relations.append({
                "object_id": obj.object_id,
                "object_name": obj.name,
                "reference_id": ref.object_id,
                "reference_name": ref.name,
                "relation": rel,
                "distance": round(ref_pos.distance_to(obj.transform.position), 3),
                "delta": [round(delta.x, 3), round(delta.y, 3), round(delta.z, 3)],
            })
        return relations

    def _compute_relation(self, delta: Vector3) -> Dict[str, str]:
        """根据位置差向量计算空间关系描述。"""
        t = self.RELATION_THRESHOLD
        relation: Dict[str, str] = {}
        # 上下（Y 轴）
        if delta.y > t:
            relation["vertical"] = "上方"
        elif delta.y < -t:
            relation["vertical"] = "下方"
        else:
            relation["vertical"] = "同高"
        # 左右（X 轴，+X 为右）
        if delta.x > t:
            relation["horizontal"] = "右侧"
        elif delta.x < -t:
            relation["horizontal"] = "左侧"
        else:
            relation["horizontal"] = "正中"
        # 前后（Z 轴，-Z 为前）
        if delta.z < -t:
            relation["depth"] = "前方"
        elif delta.z > t:
            relation["depth"] = "后方"
        else:
            relation["depth"] = "同深"
        return relation

    def generate_description(self, scene: Scene) -> str:
        """生成场景的自然语言描述。"""
        objects = list(scene.objects.values())
        if not objects:
            return f"场景「{scene.name}」当前为空。"

        parts: List[str] = []
        parts.append(f"场景「{scene.name}」包含 {len(objects)} 个物体。")

        # 按分类统计
        category_counts: Dict[str, int] = {}
        for obj in objects:
            cat = self.classify_object(obj)
            category_counts[cat] = category_counts.get(cat, 0) + 1
        cat_summary = "、".join(
            f"{cat} {count} 个" for cat, count in category_counts.items()
        )
        parts.append(f"物体分类：{cat_summary}。")

        # 列举物体名称
        names = "、".join(obj.name for obj in objects[:8])
        if len(objects) > 8:
            names += " 等"
        parts.append(f"主要物体有：{names}。")

        # 空间关系（以第一个物体为参考）
        relations = self.analyze_spatial_relations(scene)
        if relations:
            ref_name = relations[0]["reference_name"]
            rel_descs: List[str] = []
            for r in relations[:5]:
                rel = r["relation"]
                rel_descs.append(
                    f"{r['object_name']}在{ref_name}的"
                    f"{rel['vertical']}、{rel['horizontal']}、{rel['depth']}"
                    f"（距离 {r['distance']}）"
                )
            parts.append("空间关系：" + "；".join(rel_descs) + "。")

        # 光照与相机
        if scene.lights:
            parts.append(f"场景配置了 {len(scene.lights)} 个光源。")
        if scene.cameras:
            cam = self.get_active_camera_or_first(scene)
            if cam is not None:
                parts.append(f"当前视角由相机「{cam.name}」提供。")

        return "".join(parts)

    def get_active_camera_or_first(self, scene: Scene) -> Optional[Camera]:
        """获取激活相机，无则返回第一个相机。"""
        if scene.active_camera_id is not None:
            cam = scene.cameras.get(scene.active_camera_id)
            if cam is not None:
                return cam
        if scene.cameras:
            return next(iter(scene.cameras.values()))
        return None

    def summarize_scene(self, scene: Scene) -> Dict[str, Any]:
        """生成场景的结构化摘要。"""
        objects = list(scene.objects.values())
        categories: Dict[str, List[str]] = {}
        for obj in objects:
            cat = self.classify_object(obj)
            categories.setdefault(cat, []).append(obj.name)
        return {
            "scene_id": scene.scene_id,
            "name": scene.name,
            "object_count": len(objects),
            "categories": {k: len(v) for k, v in categories.items()},
            "objects_by_category": categories,
            "light_count": len(scene.lights),
            "camera_count": len(scene.cameras),
            "description": self.generate_description(scene),
        }


# ============================================================
# 手势识别
# ============================================================

class GestureType(str, Enum):
    """手势类型枚举。"""

    TAP = "tap"            # 点击
    DOUBLE_TAP = "double_tap"  # 双击
    DRAG = "drag"          # 拖拽
    PINCH = "pinch"        # 捏合（缩放）
    ROTATE = "rotate"      # 旋转
    SWIPE = "swipe"        # 滑动
    LONG_PRESS = "long_press"  # 长按
    PAN = "pan"            # 平移


@dataclass
class GestureEvent:
    """手势事件。"""

    gesture_type: GestureType
    # 触发位置（屏幕坐标或归一化坐标）
    position: Vector3 = field(default_factory=Vector3)
    # 缩放比例（pinch 手势）
    scale: float = 1.0
    # 旋转角度（弧度，rotate 手势）
    rotation: float = 0.0
    # 拖拽/滑动位移
    delta: Vector3 = field(default_factory=Vector3)
    # 持续时间（秒）
    duration: float = 0.0
    # 关联的目标物体 ID
    target_object_id: Optional[str] = None
    # 附加数据
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class GestureRecognizer:
    """手势识别器 — 手势定义、检测与事件分发。

    支持注册回调监听特定手势，并通过 ``dispatch`` 将手势事件分发给所有
    监听者。手势检测基于输入的指针轨迹与时间戳进行简单判定。
    """

    # 手势判定阈值
    TAP_MAX_DURATION: float = 0.3      # 点击最大持续时间（秒）
    TAP_MAX_MOVEMENT: float = 10.0     # 点击最大位移（像素）
    LONG_PRESS_MIN_DURATION: float = 0.6  # 长按最小持续时间
    SWIPE_MIN_DISTANCE: float = 50.0   # 滑动最小距离
    DOUBLE_TAP_INTERVAL: float = 0.3   # 双击间隔

    def __init__(self) -> None:
        # 手势类型 → 回调列表
        self._listeners: Dict[GestureType, List[Callable[[GestureEvent], None]]] = {}
        # 全局回调（监听所有手势）
        self._global_listeners: List[Callable[[GestureEvent], None]] = []
        # 上一次点击信息（用于双击判定）
        self._last_tap_time: float = 0.0
        self._last_tap_position: Optional[Vector3] = None
        # 当前进行中的手势状态
        self._active: bool = False
        self._start_time: float = 0.0
        self._start_position: Vector3 = Vector3()
        self._last_position: Vector3 = Vector3()

    # --------------------------------------------------- 监听注册
    def on(
        self,
        gesture_type: GestureType,
        callback: Callable[[GestureEvent], None],
    ) -> None:
        """注册手势监听回调。"""
        self._listeners.setdefault(gesture_type, []).append(callback)

    def on_any(self, callback: Callable[[GestureEvent], None]) -> None:
        """注册全局手势监听（接收所有手势事件）。"""
        self._global_listeners.append(callback)

    def off(
        self,
        gesture_type: GestureType,
        callback: Callable[[GestureEvent], None],
    ) -> None:
        """移除特定手势的回调。"""
        listeners = self._listeners.get(gesture_type, [])
        if callback in listeners:
            listeners.remove(callback)

    # --------------------------------------------------- 手势检测
    def begin_gesture(self, position: Vector3, timestamp: Optional[float] = None) -> None:
        """开始一次手势（指针按下）。"""
        self._active = True
        self._start_time = timestamp or time.time()
        self._start_position = position
        self._last_position = position

    def update_gesture(self, position: Vector3) -> None:
        """更新手势位置（指针移动）。"""
        if self._active:
            self._last_position = position

    def end_gesture(
        self,
        position: Vector3,
        timestamp: Optional[float] = None,
        target_object_id: Optional[str] = None,
    ) -> Optional[GestureEvent]:
        """结束手势并判定类型，返回识别出的事件（若有）。"""
        if not self._active:
            return None
        end_time = timestamp or time.time()
        self._active = False
        duration = end_time - self._start_time
        movement = self._start_position.distance_to(position)
        delta = position.sub(self._start_position)

        event: Optional[GestureEvent] = None

        # 长按判定
        if duration >= self.LONG_PRESS_MIN_DURATION and movement <= self.TAP_MAX_MOVEMENT:
            event = GestureEvent(
                gesture_type=GestureType.LONG_PRESS,
                position=position,
                duration=duration,
                target_object_id=target_object_id,
            )
        # 点击 / 双击判定
        elif movement <= self.TAP_MAX_MOVEMENT and duration <= self.TAP_MAX_DURATION:
            # 检查是否构成双击
            now = end_time
            if (
                self._last_tap_position is not None
                and (now - self._last_tap_time) <= self.DOUBLE_TAP_INTERVAL
                and self._last_tap_position.distance_to(position) <= self.TAP_MAX_MOVEMENT
            ):
                event = GestureEvent(
                    gesture_type=GestureType.DOUBLE_TAP,
                    position=position,
                    duration=duration,
                    target_object_id=target_object_id,
                )
                self._last_tap_time = 0.0
                self._last_tap_position = None
            else:
                event = GestureEvent(
                    gesture_type=GestureType.TAP,
                    position=position,
                    duration=duration,
                    target_object_id=target_object_id,
                )
                self._last_tap_time = now
                self._last_tap_position = position
        # 滑动判定
        elif movement >= self.SWIPE_MIN_DISTANCE and duration <= self.TAP_MAX_DURATION:
            event = GestureEvent(
                gesture_type=GestureType.SWIPE,
                position=position,
                delta=delta,
                duration=duration,
                target_object_id=target_object_id,
            )
        # 拖拽判定
        elif movement > self.TAP_MAX_MOVEMENT:
            event = GestureEvent(
                gesture_type=GestureType.DRAG,
                position=position,
                delta=delta,
                duration=duration,
                target_object_id=target_object_id,
            )

        if event is not None:
            self.dispatch(event)
        return event

    def detect_pinch(
        self,
        scale: float,
        position: Vector3,
        target_object_id: Optional[str] = None,
    ) -> GestureEvent:
        """检测捏合（缩放）手势并分发。"""
        event = GestureEvent(
            gesture_type=GestureType.PINCH,
            position=position,
            scale=scale,
            target_object_id=target_object_id,
        )
        self.dispatch(event)
        return event

    def detect_rotate(
        self,
        rotation: float,
        position: Vector3,
        target_object_id: Optional[str] = None,
    ) -> GestureEvent:
        """检测旋转手势并分发。"""
        event = GestureEvent(
            gesture_type=GestureType.ROTATE,
            position=position,
            rotation=rotation,
            target_object_id=target_object_id,
        )
        self.dispatch(event)
        return event

    def detect_pan(
        self,
        delta: Vector3,
        position: Vector3,
        target_object_id: Optional[str] = None,
    ) -> GestureEvent:
        """检测平移手势并分发。"""
        event = GestureEvent(
            gesture_type=GestureType.PAN,
            position=position,
            delta=delta,
            target_object_id=target_object_id,
        )
        self.dispatch(event)
        return event

    # --------------------------------------------------- 事件分发
    def dispatch(self, event: GestureEvent) -> None:
        """将手势事件分发给所有监听者。"""
        # 特定手势监听者
        for callback in self._listeners.get(event.gesture_type, []):
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("手势回调异常 (%s): %s", event.gesture_type.value, exc)
        # 全局监听者
        for callback in self._global_listeners:
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("全局手势回调异常: %s", exc)
        logger.debug("已分发手势事件: %s", event.gesture_type.value)


# ============================================================
# 语音交互
# ============================================================

@dataclass
class VoiceCommand:
    """语音命令解析结果。"""

    raw_text: str           # 原始识别文本
    intent: str             # 意图（如 "move", "rotate", "select", "create"）
    target: Optional[str]   # 目标物体名称
    parameters: Dict[str, Any] = field(default_factory=dict)
    language: str = "zh"
    confidence: float = 1.0


@dataclass
class SpatialAudio:
    """空间音频（3D 音效）定义。"""

    audio_id: str
    # 音源在 3D 空间中的位置
    position: Vector3 = field(default_factory=Vector3)
    # 音量（0~1）
    volume: float = 1.0
    # 是否循环播放
    loop: bool = False
    # 衰减距离
    ref_distance: float = 1.0
    max_distance: float = 50.0
    rolloff: float = 1.0
    # 关联的 avatar/物体
    source_id: Optional[str] = None
    # 音频数据（base64 或 URL）
    audio_data: str = ""


class VoiceInteraction:
    """语音交互 — 语音命令解析、空间语音定位与多语言支持。

    命令解析基于关键词规则匹配，将自然语言映射为结构化意图；
    空间语音定位生成 3D 音效参数，使声音随音源位置变化。
    """

    # 支持的语言
    SUPPORTED_LANGUAGES = ("zh", "en", "ja", "ko")

    # 多语言命令关键词表：language → intent → [keywords]
    _COMMAND_KEYWORDS: Dict[str, Dict[str, List[str]]] = {
        "zh": {
            "move": ["移动", "移动到", "去", "走到", "移动"],
            "rotate": ["旋转", "转动", "转"],
            "scale": ["缩放", "放大", "缩小", "变大", "变小"],
            "select": ["选择", "选中", "点击", "选"],
            "create": ["创建", "新建", "添加", "生成", "造"],
            "delete": ["删除", "移除", "去掉", "消除"],
            "look": ["看", "查看", "观察", "注视"],
            "describe": ["描述", "介绍", "说明", "是什么"],
        },
        "en": {
            "move": ["move", "go", "walk", "navigate"],
            "rotate": ["rotate", "turn", "spin"],
            "scale": ["scale", "enlarge", "shrink", "resize"],
            "select": ["select", "choose", "pick", "click"],
            "create": ["create", "add", "make", "new"],
            "delete": ["delete", "remove", "destroy"],
            "look": ["look", "view", "watch", "observe"],
            "describe": ["describe", "explain", "what is"],
        },
        "ja": {
            "move": ["移動", "動かす", "行く"],
            "rotate": ["回転", "回す"],
            "scale": ["拡大", "縮小"],
            "select": ["選択", "選ぶ"],
            "create": ["作成", "追加", "生成"],
            "delete": ["削除", "消す"],
            "look": ["見る", "確認"],
            "describe": ["説明", "紹介"],
        },
        "ko": {
            "move": ["이동", "가다"],
            "rotate": ["회전", "돌리다"],
            "scale": ["확대", "축소"],
            "select": ["선택", "고르다"],
            "create": ["생성", "추가", "만들다"],
            "delete": ["삭제", "제거"],
            "look": ["보다", "확인"],
            "describe": ["설명", "소개"],
        },
    }

    def __init__(self) -> None:
        self._default_language: str = "zh"
        # 空间音频源
        self._audio_sources: Dict[str, SpatialAudio] = {}

    # --------------------------------------------------- 命令解析
    def parse_command(self, text: str, language: Optional[str] = None) -> VoiceCommand:
        """解析语音命令文本为结构化意图。

        采用关键词匹配：遍历当前语言的关键词表，命中第一个匹配的意图。
        目标物体从文本中提取引号内容或关键词后的名词。
        """
        lang = language or self._default_language
        if lang not in self.SUPPORTED_LANGUAGES:
            lang = "zh"
        keywords_map = self._COMMAND_KEYWORDS.get(lang, self._COMMAND_KEYWORDS["zh"])

        text_stripped = text.strip()
        search_text = text_stripped.lower() if lang == "en" else text_stripped
        intent = "unknown"
        for candidate_intent, keywords in keywords_map.items():
            for kw in keywords:
                if kw in search_text:
                    intent = candidate_intent
                    break
            if intent != "unknown":
                break

        target = self._extract_target(text_stripped, lang)
        params = self._extract_parameters(text_stripped, intent, lang)

        command = VoiceCommand(
            raw_text=text_stripped,
            intent=intent,
            target=target,
            parameters=params,
            language=lang,
        )
        logger.info("语音命令解析: intent=%s target=%s lang=%s", intent, target, lang)
        return command

    def _extract_target(self, text: str, language: str) -> Optional[str]:
        """从文本中提取目标物体名称（引号内容或关键词后内容）。"""
        # 优先提取引号内的内容
        for quote_pair in ('""', "''", "「」", "『』", "“”"):
            if len(quote_pair) == 2 and quote_pair[0] in text and quote_pair[1] in text:
                start = text.find(quote_pair[0])
                end = text.find(quote_pair[1], start + 1)
                if start != -1 and end != -1 and end > start:
                    return text[start + 1:end].strip()
        # 英文：提取 "the X" 模式
        if language == "en":
            lower = text.lower()
            for prefix in ("the ", "object ", "item "):
                idx = lower.find(prefix)
                if idx != -1:
                    rest = text[idx + len(prefix):].strip().split()
                    if rest:
                        return rest[0]
        return None

    def _extract_parameters(
        self,
        text: str,
        intent: str,
        language: str,
    ) -> Dict[str, Any]:
        """从文本中提取命令参数（方向、数值等）。"""
        params: Dict[str, Any] = {}
        # 提取数字
        import re
        numbers = re.findall(r"-?\d+\.?\d*", text)
        if numbers:
            params["values"] = [float(n) for n in numbers]
        # 方向关键词
        direction_map = {
            "zh": {"前": "forward", "后": "backward", "左": "left",
                   "右": "right", "上": "up", "下": "down"},
            "en": {"forward": "forward", "backward": "backward", "left": "left",
                   "right": "right", "up": "up", "down": "down"},
        }
        dirs = direction_map.get(language, direction_map["zh"])
        search_text = text.lower() if language == "en" else text
        found_dirs = [d for kw, d in dirs.items() if kw in search_text]
        if found_dirs:
            params["direction"] = found_dirs[0]
        return params

    def set_language(self, language: str) -> bool:
        """设置默认语言。"""
        if language in self.SUPPORTED_LANGUAGES:
            self._default_language = language
            logger.info("语音交互默认语言设置为: %s", language)
            return True
        logger.warning("不支持的语言: %s", language)
        return False

    def get_supported_languages(self) -> Tuple[str, ...]:
        """获取支持的语言列表。"""
        return self.SUPPORTED_LANGUAGES

    # --------------------------------------------------- 空间语音定位
    def create_audio_source(
        self,
        position: Vector3,
        audio_data: str = "",
        volume: float = 1.0,
        source_id: Optional[str] = None,
        loop: bool = False,
    ) -> SpatialAudio:
        """创建一个空间音频源（3D 音效）。"""
        aid = f"audio_{uuid.uuid4().hex[:12]}"
        audio = SpatialAudio(
            audio_id=aid,
            position=position,
            volume=volume,
            loop=loop,
            source_id=source_id,
            audio_data=audio_data,
        )
        self._audio_sources[aid] = audio
        logger.info("创建空间音频源 %s @ %s", aid, position.to_list())
        return audio

    def compute_spatial_audio(
        self,
        listener_position: Vector3,
        audio_id: str,
    ) -> Optional[Dict[str, Any]]:
        """根据听者位置计算 3D 音效参数（音量衰减与左右声像）。

        返回包含 ``volume``、``pan``（-1 左 ~ +1 右）、``distance`` 的字典，
        前端据此应用 ``PannerNode`` / ``PositionalAudio``。
        """
        audio = self._audio_sources.get(audio_id)
        if audio is None:
            return None
        delta = audio.position.sub(listener_position)
        distance = listener_position.distance_to(audio.position)
        # 距离衰减（线性，简化模型）
        if distance >= audio.max_distance:
            volume = 0.0
        elif distance <= audio.ref_distance:
            volume = audio.volume
        else:
            ratio = (distance - audio.ref_distance) / (
                audio.max_distance - audio.ref_distance
            )
            volume = audio.volume * max(0.0, 1.0 - ratio * audio.rolloff)
        # 左右声像（基于 X 轴偏移，归一化到 -1~1）
        pan = max(-1.0, min(1.0, delta.x / max(audio.max_distance, 0.001)))
        return {
            "audio_id": audio_id,
            "volume": round(volume, 4),
            "pan": round(pan, 4),
            "distance": round(distance, 4),
            "source_position": audio.position.to_list(),
        }

    def remove_audio_source(self, audio_id: str) -> bool:
        """移除音频源。"""
        if audio_id not in self._audio_sources:
            return False
        del self._audio_sources[audio_id]
        return True

    def list_audio_sources(self) -> List[SpatialAudio]:
        """列出所有音频源。"""
        return list(self._audio_sources.values())


# ============================================================
# AR/VR 适配层
# ============================================================

class RenderMode(str, Enum):
    """渲染模式枚举。"""

    DESKTOP_VR = "desktop_vr"   # 桌面 VR（PC + 头显）
    WEBXR_AR = "webxr_ar"       # WebXR 增强现实
    WEBXR_VR = "webxr_vr"       # WebXR 虚拟现实
    MOBILE_AR = "mobile_ar"     # 移动端 AR（ARCore/ARKit）
    DESKTOP_3D = "desktop_3d"   # 桌面 3D（无 XR）


@dataclass
class DeviceInfo:
    """设备信息。"""

    device_id: str
    device_type: str = "unknown"  # "vr_headset", "ar_glasses", "mobile", "desktop"
    supports_webxr: bool = False
    supports_ar: bool = False
    supports_vr: bool = False
    # 性能等级（1=低，2=中，3=高）
    performance_level: int = 2
    # 渲染分辨率倍率
    pixel_ratio: float = 1.0
    # 视场角（度）
    fov: float = 75.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PerformanceMetrics:
    """性能指标。"""

    fps: float = 60.0
    frame_time_ms: float = 16.6
    draw_calls: int = 0
    triangles: int = 0
    # 内存占用（MB）
    memory_mb: float = 0.0
    timestamp: float = field(default_factory=time.time)


class ARVRAdapter:
    """AR/VR 适配层 — 设备检测、渲染模式切换与性能自适应。

    根据设备能力选择合适的渲染模式，并依据实时性能指标动态调整
    渲染质量（像素倍率、视场角、绘制调用），以维持目标帧率。
    """

    # 目标帧率
    TARGET_FPS: float = 60.0
    # 性能自适应阈值
    FPS_LOW_THRESHOLD: float = 45.0
    FPS_HIGH_THRESHOLD: float = 55.0

    def __init__(self) -> None:
        self._devices: Dict[str, DeviceInfo] = {}
        self._active_device_id: Optional[str] = None
        self._render_mode: RenderMode = RenderMode.DESKTOP_3D
        self._metrics_history: List[PerformanceMetrics] = []
        self._max_history: int = 60
        # 当前渲染质量参数
        self._pixel_ratio: float = 1.0
        self._fov: float = 75.0
        self._max_draw_calls: int = 2000

    # --------------------------------------------------- 设备管理
    def register_device(self, device: DeviceInfo) -> DeviceInfo:
        """注册一个设备。"""
        self._devices[device.device_id] = device
        if self._active_device_id is None:
            self._active_device_id = device.device_id
            self._apply_device_capabilities(device)
        logger.info("已注册设备 %s (type=%s, xr=%s, ar=%s, vr=%s)",
                    device.device_id, device.device_type,
                    device.supports_webxr, device.supports_ar, device.supports_vr)
        return device

    def detect_device_mode(self, device: DeviceInfo) -> RenderMode:
        """根据设备能力推断最佳渲染模式。"""
        if device.supports_webxr and device.supports_ar:
            mode = RenderMode.WEBXR_AR
        elif device.supports_webxr and device.supports_vr:
            mode = RenderMode.WEBXR_VR
        elif device.supports_ar and device.device_type == "mobile":
            mode = RenderMode.MOBILE_AR
        elif device.supports_vr and device.device_type in ("desktop", "vr_headset"):
            mode = RenderMode.DESKTOP_VR
        else:
            mode = RenderMode.DESKTOP_3D
        logger.info("设备 %s 推断渲染模式: %s", device.device_id, mode.value)
        return mode

    def set_active_device(self, device_id: str) -> bool:
        """设置当前激活设备。"""
        device = self._devices.get(device_id)
        if device is None:
            return False
        self._active_device_id = device_id
        self._apply_device_capabilities(device)
        return True

    def get_active_device(self) -> Optional[DeviceInfo]:
        """获取当前激活设备。"""
        if self._active_device_id is None:
            return None
        return self._devices.get(self._active_device_id)

    def list_devices(self) -> List[DeviceInfo]:
        """列出所有已注册设备。"""
        return list(self._devices.values())

    def _apply_device_capabilities(self, device: DeviceInfo) -> None:
        """根据设备能力应用渲染参数。"""
        self._render_mode = self.detect_device_mode(device)
        self._pixel_ratio = device.pixel_ratio
        self._fov = device.fov
        # 低性能设备限制绘制调用
        if device.performance_level <= 1:
            self._max_draw_calls = 800
        elif device.performance_level == 2:
            self._max_draw_calls = 1500
        else:
            self._max_draw_calls = 3000

    # --------------------------------------------------- 渲染模式切换
    def get_render_mode(self) -> RenderMode:
        """获取当前渲染模式。"""
        return self._render_mode

    def set_render_mode(self, mode: RenderMode) -> None:
        """手动切换渲染模式。"""
        self._render_mode = mode
        logger.info("渲染模式切换为: %s", mode.value)

    def get_render_config(self) -> Dict[str, Any]:
        """获取当前渲染配置。"""
        return {
            "render_mode": self._render_mode.value,
            "pixel_ratio": self._pixel_ratio,
            "fov": self._fov,
            "max_draw_calls": self._max_draw_calls,
            "active_device_id": self._active_device_id,
        }

    # --------------------------------------------------- 性能自适应
    def report_metrics(self, metrics: PerformanceMetrics) -> None:
        """上报性能指标，触发自适应调整。"""
        self._metrics_history.append(metrics)
        if len(self._metrics_history) > self._max_history:
            self._metrics_history.pop(0)
        self._adapt_performance()

    def _adapt_performance(self) -> None:
        """根据近期性能指标自适应调整渲染质量。"""
        if len(self._metrics_history) < 5:
            return
        # 取最近 5 帧平均 FPS
        recent = self._metrics_history[-5:]
        avg_fps = sum(m.fps for m in recent) / len(recent)
        adjusted = False

        if avg_fps < self.FPS_LOW_THRESHOLD:
            # 帧率过低，降低质量
            new_ratio = max(0.5, self._pixel_ratio - 0.1)
            if new_ratio != self._pixel_ratio:
                self._pixel_ratio = new_ratio
                adjusted = True
            # 降低视场角以减少渲染负担
            new_fov = max(60.0, self._fov - 2.0)
            if new_fov != self._fov:
                self._fov = new_fov
                adjusted = True
            if adjusted:
                logger.warning(
                    "性能自适应：FPS=%.1f 低于阈值，降低质量 (pixel_ratio=%.2f, fov=%.1f)",
                    avg_fps, self._pixel_ratio, self._fov,
                )
        elif avg_fps > self.FPS_HIGH_THRESHOLD and self._pixel_ratio < 1.0:
            # 帧率充足且当前降级，尝试恢复质量
            new_ratio = min(1.0, self._pixel_ratio + 0.05)
            if new_ratio != self._pixel_ratio:
                self._pixel_ratio = new_ratio
                logger.info(
                    "性能自适应：FPS=%.1f 充足，恢复质量 (pixel_ratio=%.2f)",
                    avg_fps, self._pixel_ratio,
                )

    def get_average_fps(self) -> float:
        """获取近期平均帧率。"""
        if not self._metrics_history:
            return self.TARGET_FPS
        return sum(m.fps for m in self._metrics_history) / len(self._metrics_history)

    def get_metrics_history(self, limit: int = 30) -> List[PerformanceMetrics]:
        """获取性能指标历史。"""
        return self._metrics_history[-limit:]


# ============================================================
# Spatial3DPlugin 插件类
# ============================================================

class Spatial3DPlugin(Plugin):
    """3D 空间交互插件 — 整合场景管理、空间对话、场景理解、手势/语音交互与 AR/VR 适配。

    作为 ``core.plugin.Plugin`` 的子类，在 ``setup`` 中初始化各子模块，
    并通过事件总线对外发布 3D 交互事件。
    """

    name = "spatial_3d"

    def __init__(self) -> None:
        super().__init__()
        self.scene_manager: SceneManager = SceneManager()
        self.dialogue: SpatialDialogue = SpatialDialogue()
        self.understanding: SceneUnderstanding = SceneUnderstanding()
        self.gestures: GestureRecognizer = GestureRecognizer()
        self.voice: VoiceInteraction = VoiceInteraction()
        self.arvr: ARVRAdapter = ARVRAdapter()

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        # 从配置读取默认语言
        cfg = ctx.config.get("spatial_3d", {}) or {}
        default_lang = cfg.get("default_language", "zh")
        self.voice.set_language(default_lang)
        # 注册默认手势监听：将手势事件转发到事件总线
        self.gestures.on_any(self._on_gesture_event)
        logger.info("spatial_3d plugin configured")

    # --------------------------------------------------- 场景管理代理
    def create_scene(self, scene_id: str, name: str, description: str = "") -> Scene:
        """创建 3D 场景。"""
        return self.scene_manager.create_scene(scene_id, name, description)

    def export_scene(self, scene_id: str) -> Optional[Dict[str, Any]]:
        """导出 Three.js 兼容的 JSON 场景数据。"""
        return self.scene_manager.export_scene_json(scene_id)

    # --------------------------------------------------- 空间对话代理
    def place_assistant(
        self,
        name: str,
        position: Vector3,
        scene_id: Optional[str] = None,
    ) -> Avatar:
        """在 3D 空间放置 AI 助手 avatar。"""
        avatar = self.dialogue.place_avatar(name, position)
        # 若提供了场景，同时在场景中创建对应物体
        if scene_id is not None:
            scene = self.scene_manager.get_scene(scene_id)
            if scene is not None:
                obj = self.scene_manager.add_object(
                    scene_id=scene_id,
                    name=name,
                    object_type=ObjectType.AVATAR,
                    transform=Transform(position=position),
                    metadata={"avatar_id": avatar.avatar_id, "category": "character"},
                )
                if obj is not None:
                    avatar.model_object_id = obj.object_id
        self.publish(
            "spatial_3d.avatar.placed",
            avatar_id=avatar.avatar_id,
            name=name,
            position=position.to_list(),
        )
        return avatar

    def send_spatial_message(
        self,
        role: str,
        content: str,
        position: Optional[Vector3] = None,
        avatar_id: Optional[str] = None,
        scene_id: Optional[str] = None,
    ) -> SpatialMessage:
        """发送空间定位消息。"""
        # 上下文感知：若未指定目标物体，自动关联最近物体
        target_object_id = None
        if scene_id is not None:
            scene = self.scene_manager.get_scene(scene_id)
            if scene is not None:
                context = self.dialogue.get_spatial_context(scene=scene)
                nearby = context.get("nearby_objects", [])
                if nearby:
                    target_object_id = nearby[0].get("object_id")
        msg = self.dialogue.send_message(
            role=role,
            content=content,
            position=position,
            avatar_id=avatar_id,
            target_object_id=target_object_id,
        )
        self.publish(
            "spatial_3d.message.sent",
            message_id=msg.message_id,
            role=role,
            position=msg.position.to_list(),
            avatar_id=avatar_id,
        )
        return msg

    # --------------------------------------------------- 场景理解代理
    def describe_scene(self, scene_id: str) -> str:
        """生成场景的自然语言描述。"""
        scene = self.scene_manager.get_scene(scene_id)
        if scene is None:
            return "场景不存在。"
        return self.understanding.generate_description(scene)

    def analyze_relations(self, scene_id: str, reference_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """分析场景中物体的空间关系。"""
        scene = self.scene_manager.get_scene(scene_id)
        if scene is None:
            return []
        return self.understanding.analyze_spatial_relations(scene, reference_id)

    # --------------------------------------------------- 手势处理
    def _on_gesture_event(self, event: GestureEvent) -> None:
        """手势事件回调 — 转发到事件总线。"""
        self.publish(
            "spatial_3d.gesture",
            gesture_type=event.gesture_type.value,
            position=event.position.to_list(),
            scale=event.scale,
            rotation=event.rotation,
            target_object_id=event.target_object_id,
        )

    # --------------------------------------------------- 语音命令处理
    def handle_voice_command(self, text: str, language: Optional[str] = None) -> VoiceCommand:
        """解析并处理语音命令。"""
        command = self.voice.parse_command(text, language)
        self.publish(
            "spatial_3d.voice.command",
            intent=command.intent,
            target=command.target,
            raw_text=command.raw_text,
            language=command.language,
        )
        return command

    # --------------------------------------------------- AR/VR 适配代理
    def register_device(self, device: DeviceInfo) -> DeviceInfo:
        """注册 AR/VR 设备。"""
        dev = self.arvr.register_device(device)
        self.publish(
            "spatial_3d.device.registered",
            device_id=device.device_id,
            render_mode=self.arvr.get_render_mode().value,
        )
        return dev

    def get_render_config(self) -> Dict[str, Any]:
        """获取当前渲染配置。"""
        return self.arvr.get_render_config()

    def report_performance(self, metrics: PerformanceMetrics) -> None:
        """上报性能指标以触发自适应。"""
        self.arvr.report_metrics(metrics)
        self.publish(
            "spatial_3d.performance",
            fps=metrics.fps,
            draw_calls=metrics.draw_calls,
            render_config=self.arvr.get_render_config(),
        )


__all__ = [
    # 基础数据结构
    "Vector3",
    "Quaternion",
    "Transform",
    # 场景数据结构
    "ObjectType",
    "GeometryType",
    "LightType",
    "CameraType",
    "Geometry",
    "Material",
    "SceneObject",
    "Camera",
    "Light",
    "Scene",
    # 管理器与功能模块
    "SceneManager",
    "Avatar",
    "SpatialMessage",
    "SpatialDialogue",
    "SceneUnderstanding",
    "GestureType",
    "GestureEvent",
    "GestureRecognizer",
    "VoiceCommand",
    "SpatialAudio",
    "VoiceInteraction",
    "RenderMode",
    "DeviceInfo",
    "PerformanceMetrics",
    "ARVRAdapter",
    # 插件入口
    "Spatial3DPlugin",
]

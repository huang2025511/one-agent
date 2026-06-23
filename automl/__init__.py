"""AutoML 自动化机器学习模块 — 端到端的自动化建模能力。

提供：
  - 特征工程（FeatureEngineer）：自动特征生成、特征选择、特征编码
  - 模型选择器（ModelSelector）：多模型自动评估与最优选择
  - 超参数优化器（HyperparameterOptimizer）：网格搜索、随机搜索、贝叶斯优化
  - 训练流水线（TrainingPipeline）：自动化训练评估、交叉验证、模型持久化
  - 模型评估器（ModelEvaluator）：分类/回归指标、特征重要性分析
  - AutoMLPlugin：整合以上能力的插件类

说明：纯 Python 实现，不依赖 sklearn 等外部库，所有算法均为简化版本。
"""

from __future__ import annotations

import itertools
import logging
import math
import pickle
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# ============================================================
# 数学与统计工具函数（纯 Python 实现）
# ============================================================

def _mean(values: Sequence[float]) -> float:
    """计算均值。"""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _variance(values: Sequence[float]) -> float:
    """计算方差。"""
    if not values:
        return 0.0
    m = _mean(values)
    return sum((v - m) ** 2 for v in values) / len(values)


def _std(values: Sequence[float]) -> float:
    """计算标准差。"""
    return math.sqrt(_variance(values))


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    """计算皮尔逊相关系数。"""
    n = min(len(x), len(y))
    if n == 0:
        return 0.0
    mx = _mean(x[:n])
    my = _mean(y[:n])
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = math.sqrt(sum((x[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((y[i] - my) ** 2 for i in range(n)))
    if dx < 1e-12 or dy < 1e-12:
        return 0.0
    return num / (dx * dy)


def _euclidean(a: Sequence[float], b: Sequence[float]) -> float:
    """计算欧氏距离。"""
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


def _sigmoid(x: float) -> float:
    """Sigmoid 函数，做了数值截断防止溢出。"""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logit(p: float) -> float:
    """Logit 函数（sigmoid 的反函数）。"""
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _normal_pdf(x: float) -> float:
    """标准正态分布概率密度函数。"""
    return math.exp(-x * x / 2.0) / math.sqrt(2 * math.pi)


def _normal_cdf(x: float) -> float:
    """标准正态分布累积分布函数。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _gini(labels: Sequence[Any]) -> float:
    """计算基尼不纯度。"""
    n = len(labels)
    if n == 0:
        return 0.0
    counts: Dict[Any, int] = {}
    for v in labels:
        counts[v] = counts.get(v, 0) + 1
    return 1.0 - sum((c / n) ** 2 for c in counts.values())


def _most_common(labels: Sequence[Any]) -> Any:
    """返回出现次数最多的元素。"""
    if not labels:
        return None
    counts: Dict[Any, int] = {}
    for v in labels:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)


def _solve_linear(A: List[List[float]], b: List[float]) -> List[float]:
    """高斯消元法求解线性方程组 Ax = b。"""
    n = len(A)
    # 构造增广矩阵
    M = [A[i][:] + [b[i]] for i in range(n)]
    for col in range(n):
        # 选列主元
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-12:
            # 奇异矩阵，加微小扰动保证可解
            M[col][col] += 1e-8
            pivot = col
        M[col], M[pivot] = M[pivot], M[col]
        piv = M[col][col]
        M[col] = [v / piv for v in M[col]]
        for r in range(n):
            if r != col:
                factor = M[r][col]
                M[r] = [a - factor * c for a, c in zip(M[r], M[col])]
    return [M[i][n] for i in range(n)]


def _mat_inverse(M: List[List[float]]) -> List[List[float]]:
    """高斯-约旦消元法求矩阵的逆。"""
    n = len(M)
    # 增广矩阵 [M | I]
    A = [M[i][:] + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(A[r][col]))
        if abs(A[pivot][col]) < 1e-12:
            A[col][col] += 1e-8
            pivot = col
        A[col], A[pivot] = A[pivot], A[col]
        piv = A[col][col]
        A[col] = [v / piv for v in A[col]]
        for r in range(n):
            if r != col:
                factor = A[r][col]
                A[r] = [a - factor * c for a, c in zip(A[r], A[col])]
    return [row[n:] for row in A]


def _kfold_indices(n: int, k: int, shuffle: bool = True, seed: int = 42) -> List[Tuple[List[int], List[int]]]:
    """生成 k 折交叉验证的索引（返回每折的训练/验证索引）。"""
    indices = list(range(n))
    if shuffle:
        rng = random.Random(seed)
        rng.shuffle(indices)
    fold_size = max(1, n // k)
    folds: List[Tuple[List[int], List[int]]] = []
    for i in range(k):
        start = i * fold_size
        end = start + fold_size if i < k - 1 else n
        val_idx = indices[start:end]
        train_idx = indices[:start] + indices[end:]
        folds.append((train_idx, val_idx))
    return folds


def _train_test_split(X: List[List[float]], y: List[Any], test_size: float = 0.2,
                      seed: int = 42) -> Tuple[List[List[float]], List[List[float]], List[Any], List[Any]]:
    """划分训练集/测试集。"""
    n = len(y)
    indices = list(range(n))
    rng = random.Random(seed)
    rng.shuffle(indices)
    n_test = max(1, int(n * test_size))
    test_idx = indices[:n_test]
    train_idx = indices[n_test:]
    X_train = [X[i] for i in train_idx]
    X_test = [X[i] for i in test_idx]
    y_train = [y[i] for i in train_idx]
    y_test = [y[i] for i in test_idx]
    return X_train, X_test, y_train, y_test


# ============================================================
# 评估指标函数
# ============================================================

def _accuracy(y_true: Sequence[Any], y_pred: Sequence[Any]) -> float:
    """准确率。"""
    n = min(len(y_true), len(y_pred))
    if n == 0:
        return 0.0
    correct = sum(1 for i in range(n) if y_true[i] == y_pred[i])
    return correct / n


def _precision(y_true: Sequence[Any], y_pred: Sequence[Any]) -> float:
    """宏平均精确率。"""
    classes = sorted(set(y_true) | set(y_pred))
    if not classes:
        return 0.0
    scores = []
    for c in classes:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != c and p == c)
        scores.append(tp / (tp + fp) if (tp + fp) > 0 else 0.0)
    return _mean(scores)


def _recall(y_true: Sequence[Any], y_pred: Sequence[Any]) -> float:
    """宏平均召回率。"""
    classes = sorted(set(y_true) | set(y_pred))
    if not classes:
        return 0.0
    scores = []
    for c in classes:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == c and p != c)
        scores.append(tp / (tp + fn) if (tp + fn) > 0 else 0.0)
    return _mean(scores)


def _f1(y_true: Sequence[Any], y_pred: Sequence[Any]) -> float:
    """宏平均 F1 分数。"""
    p = _precision(y_true, y_pred)
    r = _recall(y_true, y_pred)
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def _auc(y_true: Sequence[Any], y_score: Sequence[float]) -> float:
    """基于秩的 AUC 计算（Mann-Whitney U 统计量）。"""
    n = len(y_true)
    if n == 0:
        return 0.5
    # 将正类标记为 1
    classes = sorted(set(y_true))
    if len(classes) < 2:
        return 0.5
    pos_label = classes[-1]
    y_bin = [1 if t == pos_label else 0 for t in y_true]
    # 按分数排序并计算平均秩
    order = sorted(range(n), key=lambda i: y_score[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and y_score[order[j]] == y_score[order[i]]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0  # 1-indexed 平均秩
        for k in range(i, j):
            ranks[order[k]] = avg_rank
        i = j
    n_pos = sum(y_bin)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    sum_pos_ranks = sum(ranks[i] for i in range(n) if y_bin[i] == 1)
    return (sum_pos_ranks - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _mse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """均方误差。"""
    n = min(len(y_true), len(y_pred))
    if n == 0:
        return 0.0
    return sum((y_true[i] - y_pred[i]) ** 2 for i in range(n)) / n


def _mae(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """平均绝对误差。"""
    n = min(len(y_true), len(y_pred))
    if n == 0:
        return 0.0
    return sum(abs(y_true[i] - y_pred[i]) for i in range(n)) / n


def _r2(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """决定系数 R2。"""
    n = min(len(y_true), len(y_pred))
    if n == 0:
        return 0.0
    mean = _mean(y_true[:n])
    ss_tot = sum((y_true[i] - mean) ** 2 for i in range(n))
    ss_res = sum((y_true[i] - y_pred[i]) ** 2 for i in range(n))
    if ss_tot < 1e-12:
        return 0.0
    return 1.0 - ss_res / ss_tot


def _confusion_matrix(y_true: Sequence[Any], y_pred: Sequence[Any]) -> List[List[int]]:
    """混淆矩阵。"""
    classes = sorted(set(y_true) | set(y_pred))
    idx = {c: i for i, c in enumerate(classes)}
    matrix = [[0] * len(classes) for _ in classes]
    for t, p in zip(y_true, y_pred):
        matrix[idx[t]][idx[p]] += 1
    return matrix


def _score(scoring: str, y_true: Sequence[Any], y_pred: Sequence[Any]) -> float:
    """根据评分名称计算单一分数。"""
    if scoring == "accuracy":
        return _accuracy(y_true, y_pred)
    if scoring == "precision":
        return _precision(y_true, y_pred)
    if scoring == "recall":
        return _recall(y_true, y_pred)
    if scoring == "f1":
        return _f1(y_true, y_pred)
    if scoring == "mse":
        return _mse(y_true, y_pred)
    if scoring == "mae":
        return _mae(y_true, y_pred)
    if scoring == "r2":
        return _r2(y_true, y_pred)
    logger.warning("未知评分指标 %s，默认使用 accuracy", scoring)
    return _accuracy(y_true, y_pred)


def _maximize(scoring: str) -> bool:
    """该评分指标是否越大越好。"""
    return scoring in ("accuracy", "precision", "recall", "f1", "auc", "r2")


# ============================================================
# 数据结构定义
# ============================================================

@dataclass
class Dataset:
    """原始数据集。"""
    records: List[Dict[str, Any]] = field(default_factory=list)
    target: str = ""
    feature_types: Dict[str, str] = field(default_factory=dict)
    task: str = "auto"  # auto / classification / regression


@dataclass
class ProcessedData:
    """特征工程处理后的数据。"""
    X: List[List[float]] = field(default_factory=list)
    y: List[Any] = field(default_factory=list)
    feature_names: List[str] = field(default_factory=list)
    task: str = "regression"


@dataclass
class FeatureEngineeringConfig:
    """特征工程配置。"""
    generate_numerical: bool = True
    generate_time: bool = True
    generate_interactions: bool = True
    max_interactions: int = 10
    encoding_method: str = "onehot"  # onehot / label / target
    selection_method: str = "importance"  # variance / correlation / importance
    max_features: int = 50
    variance_threshold: float = 0.01
    correlation_threshold: float = 0.95
    top_categories: int = 10  # onehot 编码保留的最高频类别数


@dataclass
class EvaluationMetrics:
    """模型评估指标。"""
    task: str = "regression"
    accuracy: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    auc: float = 0.0
    mse: float = 0.0
    mae: float = 0.0
    r2: float = 0.0
    confusion_matrix: List[List[int]] = field(default_factory=list)
    extra: Dict[str, float] = field(default_factory=dict)


@dataclass
class ModelResult:
    """单个模型的评估结果。"""
    model_name: str = ""
    scores: List[float] = field(default_factory=list)
    mean_score: float = 0.0
    std_score: float = 0.0
    params: Dict[str, Any] = field(default_factory=dict)
    fit_time: float = 0.0


@dataclass
class OptimizationResult:
    """超参数优化结果。"""
    best_params: Dict[str, Any] = field(default_factory=dict)
    best_score: float = 0.0
    all_trials: List[Dict[str, Any]] = field(default_factory=list)
    method: str = "bayesian"
    optimization_time: float = 0.0


@dataclass
class PipelineResult:
    """训练流水线整体结果。"""
    task: str = "regression"
    best_model_name: str = ""
    best_params: Dict[str, Any] = field(default_factory=dict)
    cv_scores: List[float] = field(default_factory=list)
    test_metrics: Optional[EvaluationMetrics] = None
    feature_importance: Dict[str, float] = field(default_factory=dict)
    model_selection: List[ModelResult] = field(default_factory=list)
    optimization: Optional[OptimizationResult] = None
    model_path: str = ""
    total_time: float = 0.0
    success: bool = False
    error: str = ""


@dataclass
class AutoMLConfig:
    """AutoML 全局配置。"""
    task: str = "auto"
    cv_folds: int = 5
    scoring: str = ""
    test_size: float = 0.2
    optimize_hyperparameters: bool = True
    n_optimization_iters: int = 20
    optimization_method: str = "bayesian"  # grid / random / bayesian
    save_models: bool = True
    output_dir: str = "data/automl"
    feature_engineering: FeatureEngineeringConfig = field(default_factory=FeatureEngineeringConfig)
    random_state: int = 42


# ============================================================
# 模型实现（纯 Python 简化版本）
# ============================================================

class BaseModel:
    """所有模型的基类。"""

    name: str = "base"
    task: str = "regression"

    def __init__(self) -> None:
        self.feature_importances_: List[float] = []
        self.classes_: List[Any] = []

    def fit(self, X: List[List[float]], y: List[Any]) -> "BaseModel":
        raise NotImplementedError

    def predict(self, X: List[List[float]]) -> List[Any]:
        raise NotImplementedError

    def predict_proba(self, X: List[List[float]]) -> Optional[List[List[float]]]:
        """返回每个样本属于各类别的概率，回归模型返回 None。"""
        return None

    def get_params(self) -> Dict[str, Any]:
        """返回模型超参数。"""
        return {}

    def set_params(self, **params: Any) -> "BaseModel":
        """设置模型超参数。"""
        for k, v in params.items():
            setattr(self, k, v)
        return self

    def clone(self) -> "BaseModel":
        """克隆一个未训练的同参数模型。"""
        return type(self)(**self.get_params())


class LinearRegression(BaseModel):
    """线性回归（正规方程 + L2 正则）。"""

    name = "linear_regression"
    task = "regression"

    def __init__(self, alpha: float = 0.0) -> None:
        super().__init__()
        self.alpha = alpha
        self.coef_: List[float] = []
        self.intercept_: float = 0.0

    def get_params(self) -> Dict[str, Any]:
        return {"alpha": self.alpha}

    def fit(self, X: List[List[float]], y: List[Any]) -> "LinearRegression":
        n = len(X)
        if n == 0:
            return self
        d = len(X[0])
        # 增加偏置列
        Xb = [[1.0] + list(row) for row in X]
        # 计算 X^T X
        XtX = [[sum(Xb[i][k] * Xb[i][j] for i in range(n)) for j in range(d + 1)] for k in range(d + 1)]
        # L2 正则（不对偏置项正则）
        for i in range(1, d + 1):
            XtX[i][i] += self.alpha
        # 计算 X^T y
        Xty = [sum(Xb[i][k] * y[i] for i in range(n)) for k in range(d + 1)]
        w = _solve_linear(XtX, Xty)
        self.intercept_ = w[0]
        self.coef_ = w[1:]
        self.feature_importances_ = [abs(c) for c in self.coef_]
        return self

    def predict(self, X: List[List[float]]) -> List[float]:
        d = len(self.coef_)
        return [self.intercept_ + sum(self.coef_[j] * row[j] for j in range(d)) for row in X]


class LogisticRegression(BaseModel):
    """逻辑回归（梯度下降，支持二分类与一对多多分类）。"""

    name = "logistic_regression"
    task = "classification"

    def __init__(self, learning_rate: float = 0.1, n_iter: int = 300, alpha: float = 0.0) -> None:
        super().__init__()
        self.learning_rate = learning_rate
        self.n_iter = n_iter
        self.alpha = alpha
        self.coef_: List[float] = []
        self.intercept_: float = 0.0
        self._binary = True
        self._ovr_models: List["LogisticRegression"] = []

    def get_params(self) -> Dict[str, Any]:
        return {"learning_rate": self.learning_rate, "n_iter": self.n_iter, "alpha": self.alpha}

    def _fit_binary(self, X: List[List[float]], y01: List[float]) -> None:
        """训练二分类（y01 取值 0/1）。"""
        n = len(X)
        d = len(X[0]) if n else 0
        self.coef_ = [0.0] * d
        self.intercept_ = 0.0
        for _ in range(self.n_iter):
            grad_w = [0.0] * d
            grad_b = 0.0
            for i in range(n):
                z = self.intercept_ + sum(self.coef_[j] * X[i][j] for j in range(d))
                p = _sigmoid(z)
                err = p - y01[i]
                for j in range(d):
                    grad_w[j] += err * X[i][j]
                grad_b += err
            for j in range(d):
                self.coef_[j] -= self.learning_rate * (grad_w[j] / n + self.alpha * self.coef_[j])
            self.intercept_ -= self.learning_rate * (grad_b / n)

    def _decision_function(self, X: List[List[float]]) -> List[float]:
        d = len(self.coef_)
        return [self.intercept_ + sum(self.coef_[j] * row[j] for j in range(d)) for row in X]

    def fit(self, X: List[List[float]], y: List[Any]) -> "LogisticRegression":
        self.classes_ = sorted(set(y))
        if len(self.classes_) <= 1:
            self._binary = True
            self._fit_binary(X, [0.0] * len(y))
            return self
        if len(self.classes_) == 2:
            self._binary = True
            pos = self.classes_[1]
            self._fit_binary(X, [1.0 if v == pos else 0.0 for v in y])
        else:
            # 一对多多分类
            self._binary = False
            self._ovr_models = []
            for c in self.classes_:
                m = LogisticRegression(self.learning_rate, self.n_iter, self.alpha)
                m._fit_binary(X, [1.0 if v == c else 0.0 for v in y])
                self._ovr_models.append(m)
        self.feature_importances_ = [abs(c) for c in self.coef_]
        return self

    def predict_proba(self, X: List[List[float]]) -> List[List[float]]:
        if self._binary:
            scores = self._decision_function(X)
            return [[1 - _sigmoid(s), _sigmoid(s)] for s in scores]
        probs: List[List[float]] = []
        ovr_scores = [m._decision_function(X) for m in self._ovr_models]
        for i in range(len(X)):
            ps = [_sigmoid(ovr_scores[c][i]) for c in range(len(self.classes_))]
            total = sum(ps) or 1.0
            probs.append([p / total for p in ps])
        return probs

    def predict(self, X: List[List[float]]) -> List[Any]:
        probs = self.predict_proba(X)
        if self._binary:
            return [self.classes_[1] if p[1] >= 0.5 else self.classes_[0] for p in probs]
        return [self.classes_[max(range(len(p)), key=lambda i: p[i])] for p in probs]


class _TreeNode:
    """决策树节点。"""

    __slots__ = ("feature", "threshold", "left", "right", "value", "proba")

    def __init__(self) -> None:
        self.feature: int = -1
        self.threshold: float = 0.0
        self.left: Optional[_TreeNode] = None
        self.right: Optional[_TreeNode] = None
        self.value: Any = None
        self.proba: Optional[List[float]] = None


class DecisionTree(BaseModel):
    """CART 决策树，支持分类与回归。"""

    name = "decision_tree"

    def __init__(self, max_depth: int = 5, min_samples_split: int = 2,
                 min_samples_leaf: int = 1, task: str = "regression",
                 max_features: Optional[int] = None) -> None:
        super().__init__()
        self.max_depth = max_depth
        self.min_samples_split = min_samples_split
        self.min_samples_leaf = min_samples_leaf
        self.task = task
        self.max_features = max_features
        self.root: Optional[_TreeNode] = None
        self._n_features: int = 0
        self._importances: List[float] = []

    def get_params(self) -> Dict[str, Any]:
        return {
            "max_depth": self.max_depth,
            "min_samples_split": self.min_samples_split,
            "min_samples_leaf": self.min_samples_leaf,
            "task": self.task,
            "max_features": self.max_features,
        }

    def _impurity(self, y: List[Any]) -> float:
        if self.task == "classification":
            return _gini(y)
        return _variance([float(v) for v in y])

    def _make_leaf(self, y: List[Any]) -> _TreeNode:
        node = _TreeNode()
        if self.task == "classification":
            counts: Dict[Any, int] = {}
            for v in y:
                counts[v] = counts.get(v, 0) + 1
            n = len(y) or 1
            node.proba = [counts.get(c, 0) / n for c in self.classes_]
            node.value = _most_common(y)
        else:
            node.value = sum(float(v) for v in y) / len(y) if y else 0.0
        return node

    def _best_split(self, X: List[List[float]], y: List[Any]) -> Optional[Tuple[int, float, List[int], List[int], float]]:
        n = len(y)
        base_imp = self._impurity(y)
        best: Optional[Tuple[int, float, List[int], List[int], float]] = None
        best_gain = 0.0
        feats = list(range(self._n_features))
        if self.max_features and self.max_features < self._n_features:
            feats = random.sample(feats, self.max_features)
        for f in feats:
            values = sorted(set(X[i][f] for i in range(n)))
            for v in values:
                left_idx = [i for i in range(n) if X[i][f] <= v]
                right_idx = [i for i in range(n) if X[i][f] > v]
                if not left_idx or not right_idx:
                    continue
                left_imp = self._impurity([y[i] for i in left_idx])
                right_imp = self._impurity([y[i] for i in right_idx])
                weighted = (len(left_idx) * left_imp + len(right_idx) * right_imp) / n
                gain = base_imp - weighted
                if gain > best_gain:
                    best_gain = gain
                    best = (f, v, left_idx, right_idx, gain * n)
        return best

    def _build(self, X: List[List[float]], y: List[Any], depth: int) -> _TreeNode:
        n = len(y)
        # 终止条件
        if (depth >= self.max_depth or n < self.min_samples_split or
                (self.task == "classification" and len(set(y)) <= 1) or
                (self.task == "regression" and _variance([float(v) for v in y]) < 1e-12)):
            return self._make_leaf(y)
        best = self._best_split(X, y)
        if best is None:
            return self._make_leaf(y)
        feat, thr, left_idx, right_idx, imp_dec = best
        if len(left_idx) < self.min_samples_leaf or len(right_idx) < self.min_samples_leaf:
            return self._make_leaf(y)
        node = _TreeNode()
        node.feature = feat
        node.threshold = thr
        self._importances[feat] += imp_dec
        node.left = self._build([X[i] for i in left_idx], [y[i] for i in left_idx], depth + 1)
        node.right = self._build([X[i] for i in right_idx], [y[i] for i in right_idx], depth + 1)
        return node

    def fit(self, X: List[List[float]], y: List[Any]) -> "DecisionTree":
        self._n_features = len(X[0]) if X else 0
        self._importances = [0.0] * self._n_features
        if self.task == "classification":
            self.classes_ = sorted(set(y))
        self.root = self._build(X, y, 0)
        total = sum(self._importances)
        if total > 0:
            self._importances = [v / total for v in self._importances]
        self.feature_importances_ = list(self._importances)
        return self

    def _predict_one(self, row: List[float]) -> Any:
        node = self.root
        while node is not None and node.feature != -1:
            if row[node.feature] <= node.threshold:
                node = node.left
            else:
                node = node.right
        return node.value if node else None

    def predict(self, X: List[List[float]]) -> List[Any]:
        return [self._predict_one(row) for row in X]

    def predict_proba(self, X: List[List[float]]) -> Optional[List[List[float]]]:
        if self.task != "classification":
            return None
        result: List[List[float]] = []
        for row in X:
            node = self.root
            while node is not None and node.feature != -1:
                if row[node.feature] <= node.threshold:
                    node = node.left
                else:
                    node = node.right
            if node is not None and node.proba is not None:
                result.append(node.proba)
            else:
                result.append([1.0 / len(self.classes_)] * len(self.classes_))
        return result


class RandomForest(BaseModel):
    """随机森林（自助聚合 + 特征随机采样）。"""

    name = "random_forest"

    def __init__(self, n_estimators: int = 10, max_depth: int = 5,
                 min_samples_split: int = 2, task: str = "regression",
                 max_features: Optional[int] = None, random_state: int = 42) -> None:
        super().__init__()
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.min_samples_split = min_samples_split
        self.task = task
        self.max_features = max_features
        self.random_state = random_state
        self.trees: List[DecisionTree] = []
        self._n_features: int = 0
        self._max_features_eff: Optional[int] = None

    def get_params(self) -> Dict[str, Any]:
        return {
            "n_estimators": self.n_estimators,
            "max_depth": self.max_depth,
            "min_samples_split": self.min_samples_split,
            "task": self.task,
            "max_features": self.max_features,
            "random_state": self.random_state,
        }

    def fit(self, X: List[List[float]], y: List[Any]) -> "RandomForest":
        self._n_features = len(X[0]) if X else 0
        if self.task == "classification":
            self.classes_ = sorted(set(y))
        if self.max_features is None:
            self._max_features_eff = max(1, int(math.sqrt(self._n_features))) if self._n_features else 1
        else:
            self._max_features_eff = min(self.max_features, self._n_features)
        rng = random.Random(self.random_state)
        self.trees = []
        n = len(y)
        for _ in range(self.n_estimators):
            idx = [rng.randrange(n) for _ in range(n)] if n else []
            Xb = [X[i] for i in idx]
            yb = [y[i] for i in idx]
            tree = DecisionTree(
                max_depth=self.max_depth,
                min_samples_split=self.min_samples_split,
                task=self.task,
                max_features=self._max_features_eff,
            )
            tree.classes_ = self.classes_
            tree.fit(Xb, yb)
            self.trees.append(tree)
        # 聚合特征重要性
        importances = [0.0] * self._n_features
        for t in self.trees:
            for i, v in enumerate(t._importances):
                if i < len(importances):
                    importances[i] += v
        total = sum(importances)
        if total > 0:
            importances = [v / total for v in importances]
        self.feature_importances_ = importances
        return self

    def _avg_proba(self, X: List[List[float]]) -> List[List[float]]:
        all_proba = [t.predict_proba(X) for t in self.trees]
        result: List[List[float]] = []
        n_classes = len(self.classes_)
        for i in range(len(X)):
            avg = [0.0] * n_classes
            for t in range(len(self.trees)):
                proba = all_proba[t][i] if all_proba[t] else [1.0 / n_classes] * n_classes
                for c in range(n_classes):
                    avg[c] += proba[c]
            n = len(self.trees) or 1
            result.append([v / n for v in avg])
        return result

    def predict(self, X: List[List[float]]) -> List[Any]:
        if self.task == "classification":
            all_proba = self._avg_proba(X)
            return [self.classes_[max(range(len(p)), key=lambda i: p[i])] for p in all_proba]
        # 回归：所有树预测取平均
        preds = [t.predict(X) for t in self.trees]
        n_trees = len(self.trees) or 1
        return [sum(preds[t][i] for t in range(len(self.trees))) / n_trees for i in range(len(X))]

    def predict_proba(self, X: List[List[float]]) -> Optional[List[List[float]]]:
        if self.task != "classification":
            return None
        return self._avg_proba(X)


class GradientBoosting(BaseModel):
    """梯度提升树（回归 + 二分类；多分类采用一对多）。"""

    name = "gradient_boosting"

    def __init__(self, n_estimators: int = 50, max_depth: int = 3,
                 learning_rate: float = 0.1, task: str = "regression") -> None:
        super().__init__()
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.task = task
        self.trees: List[DecisionTree] = []
        self.init_value: float = 0.0
        self._multi = False
        self._ovr: List["GradientBoosting"] = []
        self._n_features: int = 0

    def get_params(self) -> Dict[str, Any]:
        return {
            "n_estimators": self.n_estimators,
            "max_depth": self.max_depth,
            "learning_rate": self.learning_rate,
            "task": self.task,
        }

    def _fit_regression(self, X: List[List[float]], y: List[float]) -> None:
        self.init_value = _mean(y)
        residual = [v - self.init_value for v in y]
        self.trees = []
        for _ in range(self.n_estimators):
            tree = DecisionTree(max_depth=self.max_depth, task="regression")
            tree.fit(X, residual)
            preds = tree.predict(X)
            residual = [residual[i] - self.learning_rate * preds[i] for i in range(len(residual))]
            self.trees.append(tree)

    def _fit_binary(self, X: List[List[float]], y01: List[float]) -> None:
        """二分类逻辑损失提升（y01 取值 0/1）。"""
        self.init_value = _logit(_mean(y01))
        F = [self.init_value] * len(y01)
        self.trees = []
        for _ in range(self.n_estimators):
            p = [_sigmoid(f) for f in F]
            residual = [y01[i] - p[i] for i in range(len(y01))]  # 负梯度
            tree = DecisionTree(max_depth=self.max_depth, task="regression")
            tree.fit(X, residual)
            preds = tree.predict(X)
            F = [F[i] + self.learning_rate * preds[i] for i in range(len(F))]
            self.trees.append(tree)

    def fit(self, X: List[List[float]], y: List[Any]) -> "GradientBoosting":
        self._n_features = len(X[0]) if X else 0
        if self.task == "regression":
            self._multi = False
            self._fit_regression(X, [float(v) for v in y])
        else:
            self.classes_ = sorted(set(y))
            if len(self.classes_) <= 2:
                self._multi = False
                pos = self.classes_[-1] if len(self.classes_) == 2 else self.classes_[0]
                self._fit_binary(X, [1.0 if v == pos else 0.0 for v in y])
            else:
                # 多分类：一对多
                self._multi = True
                self._ovr = []
                for c in self.classes_:
                    gb = GradientBoosting(self.n_estimators, self.max_depth, self.learning_rate, "classification")
                    gb.classes_ = self.classes_
                    gb._multi = False
                    gb._fit_binary(X, [1.0 if v == c else 0.0 for v in y])
                    self._ovr.append(gb)
        # 特征重要性：聚合所有树
        importances = [0.0] * self._n_features
        all_trees = self.trees if not self._multi else [t for gb in self._ovr for t in gb.trees]
        for t in all_trees:
            for i, v in enumerate(t._importances):
                if i < len(importances):
                    importances[i] += v
        total = sum(importances)
        if total > 0:
            importances = [v / total for v in importances]
        self.feature_importances_ = importances
        return self

    def _predict_regression(self, X: List[List[float]]) -> List[float]:
        preds = [self.init_value] * len(X)
        for tree in self.trees:
            tp = tree.predict(X)
            preds = [preds[i] + self.learning_rate * tp[i] for i in range(len(preds))]
        return preds

    def _decision_binary(self, X: List[List[float]]) -> List[float]:
        scores = [self.init_value] * len(X)
        for tree in self.trees:
            tp = tree.predict(X)
            scores = [scores[i] + self.learning_rate * tp[i] for i in range(len(scores))]
        return scores

    def predict_proba(self, X: List[List[float]]) -> Optional[List[List[float]]]:
        if self.task != "classification":
            return None
        if not self._multi:
            scores = self._decision_binary(X)
            return [[1 - _sigmoid(s), _sigmoid(s)] for s in scores]
        probs: List[List[float]] = []
        ovr_scores = [gb._decision_binary(X) for gb in self._ovr]
        for i in range(len(X)):
            ps = [_sigmoid(ovr_scores[c][i]) for c in range(len(self.classes_))]
            total = sum(ps) or 1.0
            probs.append([p / total for p in ps])
        return probs

    def predict(self, X: List[List[float]]) -> List[Any]:
        if self.task == "regression":
            return self._predict_regression(X)
        probs = self.predict_proba(X)
        if not self._multi:
            return [self.classes_[1] if p[1] >= 0.5 else self.classes_[0] for p in probs]
        return [self.classes_[max(range(len(p)), key=lambda i: p[i])] for p in probs]


class SVM(BaseModel):
    """线性 SVM（SGD 求解铰链损失），支持分类与回归。"""

    name = "svm"

    def __init__(self, C: float = 1.0, learning_rate: float = 0.01,
                 n_iter: int = 200, task: str = "classification", epsilon: float = 0.1) -> None:
        super().__init__()
        self.C = C
        self.learning_rate = learning_rate
        self.n_iter = n_iter
        self.task = task
        self.epsilon = epsilon
        self.coef_: List[float] = []
        self.intercept_: float = 0.0
        self._binary = True
        self._ovr_models: List["SVM"] = []

    def get_params(self) -> Dict[str, Any]:
        return {
            "C": self.C,
            "learning_rate": self.learning_rate,
            "n_iter": self.n_iter,
            "task": self.task,
            "epsilon": self.epsilon,
        }

    def _fit_hinge(self, X: List[List[float]], y_signed: List[float]) -> None:
        """铰链损失 SGD（y_signed 取值 ±1）。"""
        n = len(X)
        d = len(X[0]) if n else 0
        self.coef_ = [0.0] * d
        self.intercept_ = 0.0
        for _ in range(self.n_iter):
            for i in range(n):
                margin = y_signed[i] * (self.intercept_ + sum(self.coef_[j] * X[i][j] for j in range(d)))
                if margin < 1:
                    for j in range(d):
                        self.coef_[j] += self.learning_rate * (y_signed[i] * X[i][j] - self.C * self.coef_[j] / n)
                    self.intercept_ += self.learning_rate * y_signed[i]
                else:
                    for j in range(d):
                        self.coef_[j] -= self.learning_rate * (self.C * self.coef_[j] / n)

    def _fit_svr(self, X: List[List[float]], y: List[float]) -> None:
        """epsilon 不敏感损失回归 SGD。"""
        n = len(X)
        d = len(X[0]) if n else 0
        self.coef_ = [0.0] * d
        self.intercept_ = 0.0
        for _ in range(self.n_iter):
            for i in range(n):
                pred = self.intercept_ + sum(self.coef_[j] * X[i][j] for j in range(d))
                err = pred - y[i]
                if abs(err) > self.epsilon:
                    sign = 1.0 if err > 0 else -1.0
                    for j in range(d):
                        self.coef_[j] -= self.learning_rate * (sign * X[i][j] + self.C * self.coef_[j] / n)
                    self.intercept_ -= self.learning_rate * sign
                else:
                    for j in range(d):
                        self.coef_[j] -= self.learning_rate * (self.C * self.coef_[j] / n)

    def fit(self, X: List[List[float]], y: List[Any]) -> "SVM":
        if self.task == "classification":
            self.classes_ = sorted(set(y))
            if len(self.classes_) <= 1:
                self._binary = True
                self._fit_hinge(X, [1.0] * len(y))
            elif len(self.classes_) == 2:
                self._binary = True
                pos = self.classes_[1]
                self._fit_hinge(X, [1.0 if v == pos else -1.0 for v in y])
            else:
                self._binary = False
                self._ovr_models = []
                for c in self.classes_:
                    m = SVM(self.C, self.learning_rate, self.n_iter, "classification", self.epsilon)
                    m._fit_hinge(X, [1.0 if v == c else -1.0 for v in y])
                    m.classes_ = self.classes_
                    self._ovr_models.append(m)
        else:
            self._fit_svr(X, [float(v) for v in y])
        self.feature_importances_ = [abs(c) for c in self.coef_]
        return self

    def _decision_function(self, X: List[List[float]]) -> List[float]:
        d = len(self.coef_)
        return [self.intercept_ + sum(self.coef_[j] * row[j] for j in range(d)) for row in X]

    def predict_proba(self, X: List[List[float]]) -> Optional[List[List[float]]]:
        if self.task != "classification":
            return None
        if self._binary:
            scores = self._decision_function(X)
            return [[1 - _sigmoid(s), _sigmoid(s)] for s in scores]
        probs: List[List[float]] = []
        ovr_scores = [m._decision_function(X) for m in self._ovr_models]
        for i in range(len(X)):
            ps = [_sigmoid(ovr_scores[c][i]) for c in range(len(self.classes_))]
            total = sum(ps) or 1.0
            probs.append([p / total for p in ps])
        return probs

    def predict(self, X: List[List[float]]) -> List[Any]:
        if self.task == "regression":
            return self._decision_function(X)
        if self._binary:
            scores = self._decision_function(X)
            return [self.classes_[1] if s >= 0 else self.classes_[0] for s in scores]
        ovr_scores = [m._decision_function(X) for m in self._ovr_models]
        return [self.classes_[max(range(len(self.classes_)), key=lambda c: ovr_scores[c][i])]
                for i in range(len(X))]


class KNN(BaseModel):
    """K 近邻，支持分类与回归。"""

    name = "knn"

    def __init__(self, k: int = 5, task: str = "regression") -> None:
        super().__init__()
        self.k = k
        self.task = task
        self.X_train: List[List[float]] = []
        self.y_train: List[Any] = []

    def get_params(self) -> Dict[str, Any]:
        return {"k": self.k, "task": self.task}

    def fit(self, X: List[List[float]], y: List[Any]) -> "KNN":
        self.X_train = [row[:] for row in X]
        self.y_train = list(y)
        if self.task == "classification":
            self.classes_ = sorted(set(y))
        return self

    def _neighbors(self, row: List[float]) -> List[Any]:
        dists = [(_euclidean(row, self.X_train[i]), i) for i in range(len(self.X_train))]
        dists.sort(key=lambda t: t[0])
        k = max(1, min(self.k, len(self.y_train)))
        return [self.y_train[i] for _, i in dists[:k]]

    def predict(self, X: List[List[float]]) -> List[Any]:
        result: List[Any] = []
        for row in X:
            neighbors = self._neighbors(row)
            if self.task == "classification":
                result.append(_most_common(neighbors))
            else:
                result.append(sum(float(v) for v in neighbors) / len(neighbors) if neighbors else 0.0)
        return result

    def predict_proba(self, X: List[List[float]]) -> Optional[List[List[float]]]:
        if self.task != "classification":
            return None
        result: List[List[float]] = []
        for row in X:
            neighbors = self._neighbors(row)
            counts: Dict[Any, int] = {}
            for v in neighbors:
                counts[v] = counts.get(v, 0) + 1
            k = len(neighbors) or 1
            result.append([counts.get(c, 0) / k for c in self.classes_])
        return result


# ============================================================
# 高斯过程（贝叶斯优化的代理模型）
# ============================================================

class _GaussianProcess:
    """简化版高斯过程回归，使用 RBF 核。"""

    def __init__(self, length_scale: float = 1.0, noise: float = 1e-6) -> None:
        self.length_scale = length_scale
        self.noise = noise
        self.X_train: List[List[float]] = []
        self.y_train: List[float] = []
        self._K: List[List[float]] = []
        self._K_inv: List[List[float]] = []
        self._alpha: List[float] = []

    def _kernel(self, a: Sequence[float], b: Sequence[float]) -> float:
        sq_dist = sum((ai - bi) ** 2 for ai, bi in zip(a, b))
        return math.exp(-sq_dist / (2.0 * self.length_scale ** 2))

    def fit(self, X: List[List[float]], y: List[float]) -> "_GaussianProcess":
        self.X_train = [list(x) for x in X]
        self.y_train = list(y)
        n = len(X)
        self._K = [[self._kernel(X[i], X[j]) for j in range(n)] for i in range(n)]
        for i in range(n):
            self._K[i][i] += self.noise
        self._K_inv = _mat_inverse(self._K)
        self._alpha = [sum(self._K_inv[i][j] * y[j] for j in range(n)) for i in range(n)]
        return self

    def predict(self, X: List[List[float]]) -> Tuple[List[float], List[float]]:
        means: List[float] = []
        stds: List[float] = []
        n = len(self.X_train)
        for x in X:
            k = [self._kernel(x, xt) for xt in self.X_train]
            mean = sum(k[i] * self._alpha[i] for i in range(n))
            kinv_k = [sum(self._K_inv[i][j] * k[j] for j in range(n)) for i in range(n)]
            var = self._kernel(x, x) - sum(k[i] * kinv_k[i] for i in range(n))
            var = max(var, 0.0)
            means.append(mean)
            stds.append(math.sqrt(var) if var > 0 else 1e-6)
        return means, stds


def _expected_improvement(mu: float, sigma: float, best: float, xi: float = 0.01) -> float:
    """期望改进采集函数（最大化场景）。"""
    if sigma < 1e-9:
        return 0.0
    z = (mu - best - xi) / sigma
    return (mu - best - xi) * _normal_cdf(z) + sigma * _normal_pdf(z)


# ============================================================
# 交叉验证
# ============================================================

def _cross_validate(model: BaseModel, X: List[List[float]], y: List[Any],
                    k: int, scoring: str, seed: int = 42) -> List[float]:
    """k 折交叉验证，返回每折的评分。"""
    folds = _kfold_indices(len(y), k, seed=seed)
    scores: List[float] = []
    for train_idx, val_idx in folds:
        X_tr = [X[i] for i in train_idx]
        y_tr = [y[i] for i in train_idx]
        X_val = [X[i] for i in val_idx]
        y_val = [y[i] for i in val_idx]
        m = model.clone()
        m.fit(X_tr, y_tr)
        if scoring == "auc":
            proba = m.predict_proba(X_val)
            if proba:
                y_score = [p[-1] for p in proba]
            else:
                y_score = [0.5] * len(y_val)
            scores.append(_auc(y_val, y_score))
        else:
            y_pred = m.predict(X_val)
            scores.append(_score(scoring, y_val, y_pred))
    return scores


# ============================================================
# 特征工程
# ============================================================

class FeatureEngineer:
    """特征工程器 — 自动特征生成、特征选择、特征编码。"""

    def infer_types(self, records: List[Dict[str, Any]], target: str) -> Dict[str, str]:
        """推断每个特征列的类型（numerical/categorical/datetime）。"""
        types: Dict[str, str] = {}
        if not records:
            return types
        keys = [k for k in records[0].keys() if k != target]
        for key in keys:
            values = [r.get(key) for r in records if r.get(key) is not None]
            if not values:
                types[key] = "categorical"
                continue
            sample = values[0]
            if isinstance(sample, bool):
                types[key] = "categorical"
                continue
            if isinstance(sample, (int, float)):
                types[key] = "numerical"
                continue
            if isinstance(sample, str) and self._is_datetime(sample):
                types[key] = "datetime"
                continue
            # 尝试判断是否可转为数值
            num_count = sum(1 for v in values if self._to_float(v) is not None)
            if num_count / len(values) > 0.8:
                types[key] = "numerical"
            else:
                types[key] = "categorical"
        return types

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        """安全转为 float，失败返回 None。"""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _is_datetime(value: Any) -> bool:
        """判断是否为日期时间字符串。"""
        if not isinstance(value, str):
            return False
        return FeatureEngineer._parse_datetime(value) is not None

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        """解析日期时间。"""
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(float(value))
            except (OSError, ValueError, OverflowError):
                return None
        if not isinstance(value, str):
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None

    def _infer_task(self, y_raw: List[Any]) -> str:
        """根据目标值推断任务类型。"""
        numeric = True
        for v in y_raw:
            if v is None:
                continue
            if isinstance(v, bool):
                numeric = False
                break
            if isinstance(v, (int, float)):
                continue
            if self._to_float(v) is None:
                numeric = False
                break
        if not numeric:
            return "classification"
        unique = set(y_raw)
        if len(unique) <= 15:
            return "classification"
        return "regression"

    def generate_features(self, records: List[Dict[str, Any]], types: Dict[str, str],
                          target: str, config: FeatureEngineeringConfig
                          ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """自动生成新特征。"""
        new_records = [dict(r) for r in records]
        new_types = dict(types)

        # 数值特征衍生
        if config.generate_numerical:
            for name, ftype in list(types.items()):
                if ftype != "numerical":
                    continue
                for r in new_records:
                    val = self._to_float(r.get(name))
                    if val is None:
                        continue
                    if val > 0:
                        r[f"{name}_log"] = math.log1p(val)
                        r[f"{name}_sqrt"] = math.sqrt(val)
                    r[f"{name}_sq"] = val * val
                if any(f"{name}_log" in r for r in new_records):
                    new_types[f"{name}_log"] = "numerical"
                if any(f"{name}_sqrt" in r for r in new_records):
                    new_types[f"{name}_sqrt"] = "numerical"
                if any(f"{name}_sq" in r for r in new_records):
                    new_types[f"{name}_sq"] = "numerical"

        # 时间特征衍生
        if config.generate_time:
            for name, ftype in list(types.items()):
                if ftype != "datetime":
                    continue
                for r in new_records:
                    dt = self._parse_datetime(r.get(name))
                    if dt is None:
                        continue
                    r[f"{name}_year"] = dt.year
                    r[f"{name}_month"] = dt.month
                    r[f"{name}_day"] = dt.day
                    r[f"{name}_hour"] = dt.hour
                    r[f"{name}_weekday"] = dt.weekday()
                    r[f"{name}_is_weekend"] = 1 if dt.weekday() >= 5 else 0
                for suffix in ("_year", "_month", "_day", "_hour", "_weekday", "_is_weekend"):
                    new_types[f"{name}{suffix}"] = "numerical"

        # 数值特征交互（乘积）
        if config.generate_interactions:
            num_names = [n for n, t in new_types.items() if t == "numerical"]
            pairs = list(itertools.combinations(num_names, 2))[:config.max_interactions]
            for a, b in pairs:
                col = f"{a}_x_{b}"
                for r in new_records:
                    va = self._to_float(r.get(a))
                    vb = self._to_float(r.get(b))
                    if va is not None and vb is not None:
                        r[col] = va * vb
                if any(col in r for r in new_records):
                    new_types[col] = "numerical"

        logger.info("特征生成完成，共 %d 个特征", len(new_types))
        return new_records, new_types

    def _top_categories(self, values: List[Any], top_n: int) -> List[Any]:
        """取出现频率最高的 top_n 个类别。"""
        counts: Dict[Any, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        ranked = sorted(counts, key=counts.get, reverse=True)
        return ranked[:top_n]

    def _target_encoding(self, values: List[Any], y: List[Any]) -> Tuple[Dict[Any, float], float]:
        """目标编码：类别映射为目标均值。"""
        sums: Dict[Any, float] = {}
        counts: Dict[Any, int] = {}
        for v, t in zip(values, y):
            tv = float(t)
            sums[v] = sums.get(v, 0.0) + tv
            counts[v] = counts.get(v, 0) + 1
        mapping = {v: sums[v] / counts[v] for v in sums}
        global_mean = _mean([float(t) for t in y]) if y else 0.0
        return mapping, global_mean

    def encode(self, records: List[Dict[str, Any]], types: Dict[str, str],
               y: List[Any], config: FeatureEngineeringConfig
               ) -> Tuple[List[List[float]], List[str]]:
        """特征编码，返回数值矩阵与特征名。"""
        columns: List[Tuple[str, List[float]]] = []
        n = len(records)
        for name, ftype in types.items():
            values = [r.get(name) for r in records]
            if ftype == "datetime":
                continue  # 已在生成阶段提取为数值分量
            if ftype == "numerical":
                num_values = [self._to_float(v) for v in values]
                mean = _mean([v for v in num_values if v is not None])
                col = [v if v is not None else mean for v in num_values]
                columns.append((name, col))
            else:  # categorical
                if config.encoding_method == "onehot":
                    cats = self._top_categories(values, config.top_categories)
                    for c in cats:
                        col = [1.0 if v == c else 0.0 for v in values]
                        columns.append((f"{name}={c}", col))
                elif config.encoding_method == "label":
                    cats = sorted(set(values))
                    mapping = {c: i for i, c in enumerate(cats)}
                    col = [float(mapping.get(v, -1)) for v in values]
                    columns.append((name, col))
                elif config.encoding_method == "target":
                    mapping, global_mean = self._target_encoding(values, y)
                    col = [mapping.get(v, global_mean) for v in values]
                    columns.append((name, col))
                else:
                    cats = sorted(set(values))
                    mapping = {c: i for i, c in enumerate(cats)}
                    col = [float(mapping.get(v, -1)) for v in values]
                    columns.append((name, col))
        feature_names = [c[0] for c in columns]
        X = [[columns[j][1][i] for j in range(len(columns))] for i in range(n)]
        logger.info("特征编码完成（%s），共 %d 个特征", config.encoding_method, len(feature_names))
        return X, feature_names

    def select_features(self, X: List[List[float]], y: List[Any], feature_names: List[str],
                        config: FeatureEngineeringConfig) -> Tuple[List[List[float]], List[str]]:
        """特征选择。"""
        if not X or not feature_names:
            return X, feature_names
        n_features = len(feature_names)
        method = config.selection_method
        keep = list(range(n_features))

        if method == "variance":
            # 方差阈值过滤
            keep = []
            for j in range(n_features):
                col = [X[i][j] for i in range(len(X))]
                if _variance(col) >= config.variance_threshold:
                    keep.append(j)
        elif method == "correlation":
            # 相关性过滤：移除高度相关的冗余特征
            keep = list(range(n_features))
            drop = set()
            for a in range(n_features):
                if a in drop:
                    continue
                col_a = [X[i][a] for i in range(len(X))]
                for b in range(a + 1, n_features):
                    if b in drop:
                        continue
                    col_b = [X[i][b] for i in range(len(X))]
                    if abs(_pearson(col_a, col_b)) >= config.correlation_threshold:
                        drop.add(b)
            keep = [j for j in keep if j not in drop]
        elif method == "importance":
            # 重要性选择：按与目标的相关性排序（分类目标先做标签编码）
            y_num = self._encode_target(y)
            scored = []
            for j in range(n_features):
                col = [X[i][j] for i in range(len(X))]
                scored.append((j, abs(_pearson(col, y_num))))
            scored.sort(key=lambda t: t[1], reverse=True)
            keep = [j for j, _ in scored[:config.max_features]]
        else:
            keep = list(range(n_features))

        # 限制最大特征数
        if len(keep) > config.max_features:
            keep = keep[:config.max_features]

        new_X = [[X[i][j] for j in keep] for i in range(len(X))]
        new_names = [feature_names[j] for j in keep]
        logger.info("特征选择完成（%s），保留 %d/%d 个特征", method, len(keep), n_features)
        return new_X, new_names

    @staticmethod
    def _encode_target(y: List[Any]) -> List[float]:
        """将目标值编码为数值（用于特征重要性计算），数值型直接转换，类别型做标签编码。"""
        numeric: List[float] = []
        for v in y:
            fv = FeatureEngineer._to_float(v)
            numeric.append(fv if fv is not None else 0.0)
        # 若全部可转为数值则直接返回
        if all(FeatureEngineer._to_float(v) is not None for v in y):
            return numeric
        # 类别型：标签编码
        classes = sorted(set(y))
        mapping = {c: i for i, c in enumerate(classes)}
        return [float(mapping.get(v, 0)) for v in y]

    def transform(self, dataset: Dataset, config: FeatureEngineeringConfig) -> ProcessedData:
        """完整特征工程流水线。"""
        records = [dict(r) for r in dataset.records]
        target = dataset.target
        # 推断类型
        types = dict(dataset.feature_types) if dataset.feature_types else self.infer_types(records, target)
        # 提取目标
        y_raw = [r.get(target) for r in records]
        # 推断任务
        task = dataset.task
        if task == "auto":
            task = self._infer_task(y_raw)
        # 数值化目标
        if task == "regression":
            y = [self._to_float(v) or 0.0 for v in y_raw]
        else:
            y = list(y_raw)
        # 生成特征
        records, types = self.generate_features(records, types, target, config)
        # 编码
        X, feature_names = self.encode(records, types, y, config)
        # 特征选择
        X, feature_names = self.select_features(X, y, feature_names, config)
        return ProcessedData(X=X, y=y, feature_names=feature_names, task=task)


# ============================================================
# 模型评估器
# ============================================================

class ModelEvaluator:
    """模型评估器 — 分类/回归指标与特征重要性分析。"""

    def evaluate(self, y_true: List[Any], y_pred: List[Any],
                 y_score: Optional[List[List[float]]] = None,
                 task: str = "regression") -> EvaluationMetrics:
        """综合评估。"""
        if task == "classification":
            return self.evaluate_classification(y_true, y_pred, y_score)
        return self.evaluate_regression(y_true, y_pred)

    def evaluate_classification(self, y_true: List[Any], y_pred: List[Any],
                                y_score: Optional[List[List[float]]] = None) -> EvaluationMetrics:
        """分类指标评估。"""
        metrics = EvaluationMetrics(task="classification")
        metrics.accuracy = _accuracy(y_true, y_pred)
        metrics.precision = _precision(y_true, y_pred)
        metrics.recall = _recall(y_true, y_pred)
        metrics.f1 = _f1(y_true, y_pred)
        metrics.confusion_matrix = _confusion_matrix(y_true, y_pred)
        # AUC（仅二分类且有概率输出时计算）
        classes = sorted(set(y_true))
        if y_score is not None and len(classes) == 2:
            pos_idx = 1
            y_score_pos = [s[pos_idx] if len(s) > pos_idx else 0.5 for s in y_score]
            metrics.auc = _auc(y_true, y_score_pos)
        return metrics

    def evaluate_regression(self, y_true: List[Any], y_pred: List[Any]) -> EvaluationMetrics:
        """回归指标评估。"""
        yt = [float(v) for v in y_true]
        yp = [float(v) for v in y_pred]
        metrics = EvaluationMetrics(task="regression")
        metrics.mse = _mse(yt, yp)
        metrics.mae = _mae(yt, yp)
        metrics.r2 = _r2(yt, yp)
        return metrics

    def feature_importance(self, model: BaseModel, X: List[List[float]], y: List[Any],
                           feature_names: List[str], scoring: Optional[str] = None,
                           task: str = "regression") -> Dict[str, float]:
        """特征重要性分析：优先使用模型自带重要性，否则使用排列重要性。"""
        # 模型自带重要性
        builtin = getattr(model, "feature_importances_", None)
        if isinstance(builtin, list) and builtin:
            return {feature_names[i]: builtin[i] for i in range(min(len(builtin), len(feature_names)))}
        if isinstance(builtin, dict) and builtin:
            return dict(builtin)
        # 排列重要性
        if scoring is None:
            scoring = "accuracy" if task == "classification" else "r2"
        baseline = _score(scoring, y, model.predict(X))
        n_features = len(X[0]) if X else 0
        result: Dict[str, float] = {}
        rng = random.Random(42)
        for f in range(n_features):
            X_perm = [row[:] for row in X]
            col = [X_perm[i][f] for i in range(len(X_perm))]
            rng.shuffle(col)
            for i in range(len(X_perm)):
                X_perm[i][f] = col[i]
            perm_score = _score(scoring, y, model.predict(X_perm))
            name = feature_names[f] if f < len(feature_names) else f"x{f}"
            result[name] = baseline - perm_score
        return result


# ============================================================
# 模型选择器
# ============================================================

class ModelSelector:
    """模型选择器 — 多模型自动评估与最优选择。"""

    def __init__(self, evaluator: Optional[ModelEvaluator] = None) -> None:
        self.evaluator = evaluator or ModelEvaluator()

    def get_candidates(self, task: str) -> List[BaseModel]:
        """根据任务类型返回候选模型列表。"""
        if task == "classification":
            return [
                LogisticRegression(),
                DecisionTree(task="classification"),
                RandomForest(task="classification"),
                GradientBoosting(task="classification"),
                SVM(task="classification"),
                KNN(task="classification"),
            ]
        return [
            LinearRegression(),
            DecisionTree(task="regression"),
            RandomForest(task="regression"),
            GradientBoosting(task="regression"),
            SVM(task="regression"),
            KNN(task="regression"),
        ]

    def select(self, X: List[List[float]], y: List[Any], task: str,
               cv_folds: int = 5, scoring: Optional[str] = None,
               seed: int = 42) -> List[ModelResult]:
        """对所有候选模型做交叉验证评估，按得分排序返回。"""
        if scoring is None:
            scoring = "accuracy" if task == "classification" else "r2"
        candidates = self.get_candidates(task)
        results: List[ModelResult] = []
        for model in candidates:
            start = time.time()
            try:
                scores = _cross_validate(model, X, y, cv_folds, scoring, seed=seed)
                results.append(ModelResult(
                    model_name=model.name,
                    scores=scores,
                    mean_score=_mean(scores),
                    std_score=_std(scores),
                    params=model.get_params(),
                    fit_time=time.time() - start,
                ))
                logger.info("模型 %s 评估完成，%s=%.4f", model.name, scoring, _mean(scores))
            except Exception as exc:  # noqa: BLE001
                logger.warning("模型 %s 评估失败: %s", model.name, exc)
        # 排序：越大越好的指标降序，否则升序
        results.sort(key=lambda r: r.mean_score, reverse=_maximize(scoring))
        return results


# ============================================================
# 超参数优化器
# ============================================================

class HyperparameterOptimizer:
    """超参数优化器 — 网格搜索、随机搜索、贝叶斯优化。"""

    def __init__(self, evaluator: Optional[ModelEvaluator] = None) -> None:
        self.evaluator = evaluator or ModelEvaluator()

    # 默认搜索空间 ------------------------------------------------------
    def default_space(self, model: BaseModel) -> Dict[str, Dict[str, Any]]:
        """返回模型的默认超参数搜索空间。"""
        name = model.name
        if name == "linear_regression":
            return {"alpha": {"type": "uniform", "low": 0.0, "high": 1.0}}
        if name == "logistic_regression":
            return {
                "learning_rate": {"type": "uniform", "low": 0.01, "high": 1.0},
                "n_iter": {"type": "int", "low": 100, "high": 500},
                "alpha": {"type": "uniform", "low": 0.0, "high": 1.0},
            }
        if name == "decision_tree":
            return {
                "max_depth": {"type": "int", "low": 2, "high": 10},
                "min_samples_split": {"type": "int", "low": 2, "high": 10},
            }
        if name == "random_forest":
            return {
                "n_estimators": {"type": "int", "low": 5, "high": 50},
                "max_depth": {"type": "int", "low": 2, "high": 10},
            }
        if name == "gradient_boosting":
            return {
                "n_estimators": {"type": "int", "low": 20, "high": 100},
                "max_depth": {"type": "int", "low": 2, "high": 6},
                "learning_rate": {"type": "uniform", "low": 0.01, "high": 0.3},
            }
        if name == "svm":
            return {
                "C": {"type": "uniform", "low": 0.1, "high": 10.0},
                "learning_rate": {"type": "uniform", "low": 0.001, "high": 0.1},
            }
        if name == "knn":
            return {"k": {"type": "int", "low": 1, "high": 15}}
        return {}

    # 编码/采样 ---------------------------------------------------------
    @staticmethod
    def _encode_params(params: Dict[str, Any], space: Dict[str, Dict[str, Any]]) -> List[float]:
        """将参数字典编码为 [0,1] 向量（供贝叶斯优化使用）。"""
        vec: List[float] = []
        for name, spec in space.items():
            val = params.get(name)
            t = spec["type"]
            if t == "categorical":
                choices = spec["choices"]
                idx = choices.index(val) if val in choices else 0
                vec.append(idx / max(1, len(choices) - 1))
            else:
                lo, hi = spec["low"], spec["high"]
                vec.append((val - lo) / (hi - lo) if hi > lo else 0.0)
        return vec

    @staticmethod
    def _sample_params(space: Dict[str, Dict[str, Any]], rng: random.Random) -> Dict[str, Any]:
        """从搜索空间随机采样一组参数。"""
        params: Dict[str, Any] = {}
        for name, spec in space.items():
            t = spec["type"]
            if t == "categorical":
                params[name] = rng.choice(spec["choices"])
            elif t == "int":
                params[name] = rng.randint(spec["low"], spec["high"])
            elif t == "uniform":
                params[name] = rng.uniform(spec["low"], spec["high"])
        return params

    def _evaluate_params(self, model: BaseModel, params: Dict[str, Any], X: List[List[float]],
                         y: List[Any], cv_folds: int, scoring: str, seed: int) -> float:
        """评估一组超参数的交叉验证得分。"""
        m = model.clone()
        m.set_params(**params)
        scores = _cross_validate(m, X, y, cv_folds, scoring, seed=seed)
        return _mean(scores)

    # 网格搜索 ----------------------------------------------------------
    def grid_search(self, model: BaseModel, param_grid: Dict[str, List[Any]], X: List[List[float]],
                    y: List[Any], cv_folds: int = 5, scoring: Optional[str] = None,
                    seed: int = 42) -> OptimizationResult:
        """网格搜索：穷举所有参数组合。"""
        if scoring is None:
            scoring = "r2"
        start = time.time()
        keys = list(param_grid.keys())
        value_lists = [param_grid[k] for k in keys]
        trials: List[Dict[str, Any]] = []
        best_score = None
        best_params: Dict[str, Any] = {}
        for combo in itertools.product(*value_lists):
            params = dict(zip(keys, combo))
            score = self._evaluate_params(model, params, X, y, cv_folds, scoring, seed)
            trials.append({"params": params, "score": score})
            if best_score is None or (_maximize(scoring) and score > best_score) or \
                    (not _maximize(scoring) and score < best_score):
                best_score = score
                best_params = params
        logger.info("网格搜索完成，共 %d 组，最优得分 %.4f", len(trials), best_score or 0.0)
        return OptimizationResult(
            best_params=best_params,
            best_score=best_score or 0.0,
            all_trials=trials,
            method="grid",
            optimization_time=time.time() - start,
        )

    # 随机搜索 ----------------------------------------------------------
    def random_search(self, model: BaseModel, space: Dict[str, Dict[str, Any]], X: List[List[float]],
                      y: List[Any], n_iter: int = 20, cv_folds: int = 5,
                      scoring: Optional[str] = None, seed: int = 42) -> OptimizationResult:
        """随机搜索：随机采样参数组合。"""
        if scoring is None:
            scoring = "r2"
        start = time.time()
        rng = random.Random(seed)
        trials: List[Dict[str, Any]] = []
        best_score = None
        best_params: Dict[str, Any] = {}
        for _ in range(n_iter):
            params = self._sample_params(space, rng)
            score = self._evaluate_params(model, params, X, y, cv_folds, scoring, seed)
            trials.append({"params": params, "score": score})
            if best_score is None or (_maximize(scoring) and score > best_score) or \
                    (not _maximize(scoring) and score < best_score):
                best_score = score
                best_params = params
        logger.info("随机搜索完成，共 %d 次，最优得分 %.4f", n_iter, best_score or 0.0)
        return OptimizationResult(
            best_params=best_params,
            best_score=best_score or 0.0,
            all_trials=trials,
            method="random",
            optimization_time=time.time() - start,
        )

    # 贝叶斯优化 --------------------------------------------------------
    def bayesian_optimize(self, model: BaseModel, space: Dict[str, Dict[str, Any]], X: List[List[float]],
                          y: List[Any], n_iter: int = 20, cv_folds: int = 5,
                          scoring: Optional[str] = None, seed: int = 42) -> OptimizationResult:
        """贝叶斯优化：基于高斯过程代理模型与期望改进采集函数。"""
        if scoring is None:
            scoring = "r2"
        start = time.time()
        rng = random.Random(seed)
        higher = _maximize(scoring)
        # 初始随机采样
        init_n = min(5, max(1, n_iter))
        tried: List[Dict[str, Any]] = []
        raw_scores: List[float] = []
        for _ in range(init_n):
            params = self._sample_params(space, rng)
            score = self._evaluate_params(model, params, X, y, cv_folds, scoring, seed)
            tried.append(params)
            raw_scores.append(score)
        # 迭代优化（内部统一最大化，对越小越好的指标取负）
        for _ in range(max(0, n_iter - init_n)):
            eff_scores = [s if higher else -s for s in raw_scores]
            gp = _GaussianProcess()
            X_enc = [self._encode_params(p, space) for p in tried]
            gp.fit(X_enc, eff_scores)
            best_eff = max(eff_scores)
            # 生成候选并选择期望改进最大者
            candidates = [self._sample_params(space, rng) for _ in range(100)]
            cand_enc = [self._encode_params(p, space) for p in candidates]
            means, stds = gp.predict(cand_enc)
            eis = [_expected_improvement(means[i], stds[i], best_eff) for i in range(len(candidates))]
            best_cand = candidates[max(range(len(eis)), key=lambda i: eis[i])]
            score = self._evaluate_params(model, best_cand, X, y, cv_folds, scoring, seed)
            tried.append(best_cand)
            raw_scores.append(score)
        # 选出最优
        best_idx = max(range(len(raw_scores)), key=lambda i: raw_scores[i]) if higher else \
            min(range(len(raw_scores)), key=lambda i: raw_scores[i])
        trials = [{"params": tried[i], "score": raw_scores[i]} for i in range(len(tried))]
        logger.info("贝叶斯优化完成，共 %d 次，最优得分 %.4f", len(trials), raw_scores[best_idx])
        return OptimizationResult(
            best_params=tried[best_idx],
            best_score=raw_scores[best_idx],
            all_trials=trials,
            method="bayesian",
            optimization_time=time.time() - start,
        )

    # 统一入口 ----------------------------------------------------------
    def optimize(self, model: BaseModel, space: Optional[Dict[str, Dict[str, Any]]], X: List[List[float]],
                 y: List[Any], method: str = "bayesian", n_iter: int = 20, cv_folds: int = 5,
                 scoring: Optional[str] = None, seed: int = 42) -> OptimizationResult:
        """根据方法名分发到具体的优化策略。"""
        if space is None:
            space = self.default_space(model)
        if not space:
            # 无搜索空间，直接评估默认参数
            score = self._evaluate_params(model, {}, X, y, cv_folds, scoring or "r2", seed)
            return OptimizationResult(best_params={}, best_score=score, all_trials=[{"params": {}, "score": score}],
                                      method=method, optimization_time=0.0)
        if method == "grid":
            # 网格搜索需要离散值，将空间离散化
            grid: Dict[str, List[Any]] = {}
            for name, spec in space.items():
                t = spec["type"]
                if t == "categorical":
                    grid[name] = list(spec["choices"])
                elif t == "int":
                    grid[name] = list(range(int(spec["low"]), int(spec["high"]) + 1))
                else:
                    steps = min(5, max(2, n_iter))
                    lo, hi = spec["low"], spec["high"]
                    grid[name] = [lo + (hi - lo) * i / (steps - 1) for i in range(steps)]
            return self.grid_search(model, grid, X, y, cv_folds, scoring, seed)
        if method == "random":
            return self.random_search(model, space, X, y, n_iter, cv_folds, scoring, seed)
        return self.bayesian_optimize(model, space, X, y, n_iter, cv_folds, scoring, seed)


# ============================================================
# 训练流水线
# ============================================================

class TrainingPipeline:
    """自动化训练评估流水线。"""

    def __init__(self, config: Optional[AutoMLConfig] = None) -> None:
        self.config = config or AutoMLConfig()
        self.feature_engineer = FeatureEngineer()
        self.evaluator = ModelEvaluator()
        self.selector = ModelSelector(self.evaluator)
        self.optimizer = HyperparameterOptimizer(self.evaluator)

    def cross_validate(self, model: BaseModel, X: List[List[float]], y: List[Any],
                       k: int = 5, scoring: Optional[str] = None, seed: int = 42) -> List[float]:
        """交叉验证。"""
        if scoring is None:
            scoring = "accuracy" if model.task == "classification" else "r2"
        return _cross_validate(model, X, y, k, scoring, seed)

    def save_model(self, model: BaseModel, path: str) -> bool:
        """模型持久化（pickle）。"""
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                pickle.dump(model, f)
            logger.info("模型已保存到 %s", path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("模型保存失败: %s", exc)
            return False

    def load_model(self, path: str) -> Optional[BaseModel]:
        """加载持久化模型。"""
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.error("模型加载失败: %s", exc)
            return None

    def run(self, dataset: Dataset, config: Optional[AutoMLConfig] = None) -> PipelineResult:
        """执行完整的自动化训练评估流水线。"""
        cfg = config or self.config
        start = time.time()
        result = PipelineResult()
        try:
            # 1. 特征工程
            logger.info("=== AutoML 流水线启动 ===")
            processed = self.feature_engineer.transform(dataset, cfg.feature_engineering)
            result.task = processed.task
            if not processed.X:
                raise ValueError("特征工程后数据为空")
            scoring = cfg.scoring or ("accuracy" if processed.task == "classification" else "r2")

            # 2. 划分训练/测试集
            X_train, X_test, y_train, y_test = _train_test_split(
                processed.X, processed.y, test_size=cfg.test_size, seed=cfg.random_state
            )

            # 3. 模型选择
            logger.info("=== 模型选择阶段 ===")
            selection = self.selector.select(X_train, y_train, processed.task,
                                             cv_folds=cfg.cv_folds, scoring=scoring, seed=cfg.random_state)
            result.model_selection = selection
            if not selection:
                raise ValueError("没有可用的模型")
            best_result = selection[0]
            result.best_model_name = best_result.model_name
            logger.info("最优模型: %s（%s=%.4f）", best_result.model_name, scoring, best_result.mean_score)

            # 4. 超参数优化
            best_params: Dict[str, Any] = dict(best_result.params)
            if cfg.optimize_hyperparameters:
                logger.info("=== 超参数优化阶段（%s）===", cfg.optimization_method)
                # 重建最优模型实例
                candidates = self.selector.get_candidates(processed.task)
                best_model = next((m for m in candidates if m.name == best_result.model_name), candidates[0])
                opt = self.optimizer.optimize(
                    best_model, None, X_train, y_train,
                    method=cfg.optimization_method, n_iter=cfg.n_optimization_iters,
                    cv_folds=cfg.cv_folds, scoring=scoring, seed=cfg.random_state,
                )
                result.optimization = opt
                best_params = dict(opt.best_params)
                # 保留 task 等必要参数
                if "task" not in best_params and hasattr(best_model, "task"):
                    best_params["task"] = best_model.task
                logger.info("超参数优化完成，最优得分 %.4f", opt.best_score)

            # 5. 最终训练（在全部训练集上）
            logger.info("=== 最终训练阶段 ===")
            candidates = self.selector.get_candidates(processed.task)
            final_model = next((m for m in candidates if m.name == result.best_model_name), candidates[0])
            final_model.set_params(**best_params)
            final_model.fit(X_train, y_train)

            # 6. 评估
            y_pred = final_model.predict(X_test)
            y_score = final_model.predict_proba(X_test) if processed.task == "classification" else None
            result.test_metrics = self.evaluator.evaluate(y_test, y_pred, y_score, processed.task)
            result.cv_scores = best_result.scores

            # 7. 特征重要性
            try:
                result.feature_importance = self.evaluator.feature_importance(
                    final_model, X_test, y_test, processed.feature_names, scoring, processed.task
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("特征重要性分析失败: %s", exc)

            # 8. 模型持久化
            if cfg.save_models:
                model_path = str(Path(cfg.output_dir) / f"{result.best_model_name}_{int(time.time())}.pkl")
                if self.save_model(final_model, model_path):
                    result.model_path = model_path

            result.best_params = best_params
            result.total_time = time.time() - start
            result.success = True
            logger.info("=== AutoML 流水线完成，耗时 %.2fs ===", result.total_time)
            return result
        except Exception as exc:  # noqa: BLE001
            result.success = False
            result.error = str(exc)
            result.total_time = time.time() - start
            logger.error("AutoML 流水线失败: %s", exc, exc_info=True)
            return result


# ============================================================
# AutoML 插件
# ============================================================

class AutoMLPlugin(Plugin):
    """AutoML 插件 — 整合特征工程、模型选择、超参数优化、训练流水线与评估。"""

    name = "automl"
    depends_on: List[str] = []
    load_priority = 0

    def __init__(self) -> None:
        super().__init__()
        self._config = AutoMLConfig()
        self._pipeline: Optional[TrainingPipeline] = None

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("automl", {}) if hasattr(ctx, "config") else {}
        cfg = cfg or {}
        fe_cfg = cfg.get("feature_engineering", {}) or {}
        self._config = AutoMLConfig(
            task=cfg.get("task", "auto"),
            cv_folds=cfg.get("cv_folds", 5),
            scoring=cfg.get("scoring", ""),
            test_size=float(cfg.get("test_size", 0.2)),
            optimize_hyperparameters=cfg.get("optimize_hyperparameters", True),
            n_optimization_iters=cfg.get("n_optimization_iters", 20),
            optimization_method=cfg.get("optimization_method", "bayesian"),
            save_models=cfg.get("save_models", True),
            output_dir=cfg.get("output_dir", "data/automl"),
            feature_engineering=FeatureEngineeringConfig(
                generate_numerical=fe_cfg.get("generate_numerical", True),
                generate_time=fe_cfg.get("generate_time", True),
                generate_interactions=fe_cfg.get("generate_interactions", True),
                max_interactions=fe_cfg.get("max_interactions", 10),
                encoding_method=fe_cfg.get("encoding_method", "onehot"),
                selection_method=fe_cfg.get("selection_method", "importance"),
                max_features=fe_cfg.get("max_features", 50),
                variance_threshold=float(fe_cfg.get("variance_threshold", 0.01)),
                correlation_threshold=float(fe_cfg.get("correlation_threshold", 0.95)),
                top_categories=fe_cfg.get("top_categories", 10),
            ),
            random_state=cfg.get("random_state", 42),
        )
        Path(self._config.output_dir).mkdir(parents=True, exist_ok=True)
        self._pipeline = TrainingPipeline(self._config)
        logger.info("automl plugin configured")

    def run(self, dataset: Dataset, config: Optional[AutoMLConfig] = None) -> PipelineResult:
        """执行自动化机器学习流水线。"""
        if self._pipeline is None:
            self._pipeline = TrainingPipeline(self._config)
        return self._pipeline.run(dataset, config)

    def get_feature_engineer(self) -> FeatureEngineer:
        """获取特征工程器。"""
        return self._pipeline.feature_engineer if self._pipeline else FeatureEngineer()

    def get_evaluator(self) -> ModelEvaluator:
        """获取模型评估器。"""
        return self._pipeline.evaluator if self._pipeline else ModelEvaluator()

    def get_selector(self) -> ModelSelector:
        """获取模型选择器。"""
        return self._pipeline.selector if self._pipeline else ModelSelector()

    def get_optimizer(self) -> HyperparameterOptimizer:
        """获取超参数优化器。"""
        return self._pipeline.optimizer if self._pipeline else HyperparameterOptimizer()

    async def stop(self) -> None:
        logger.info("automl plugin stopped")
        await super().stop()
